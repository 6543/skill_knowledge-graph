#!/usr/bin/env python3
"""Persistent RDF topology plus SQLite FTS content for knowledge graphs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from collections import Counter, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import pyoxigraph as ox

DEFAULT_NS = os.environ.get("KG_NAMESPACE", "urn:kg:")
RDF_TYPE = ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
XSD = "http://www.w3.org/2001/XMLSchema#"
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORD = re.compile(r"[^\W_][\w.:-]*", re.UNICODE)
JSON_TYPE = ox.NamedNode("urn:graph-skill-db:json")

PACKAGE_SUFFIX = ".graph-skill-db"
PACKAGE_FORMAT = "graph-skill-db"
# Version 4 adds the optional usage.jsonl member. A package without usage rows
# is still written as version 3, so older readers keep accepting it.
PACKAGE_VERSION = 4
PACKAGE_MEMBERS = {"manifest.json", "nodes.jsonl", "edges.jsonl"}
USAGE_MEMBER = "usage.jsonl"
# Counters halve once this many events (or 10 per node, if more) were counted:
# TinyLFU-style aging, so counts favour recent use and rows of forgotten items
# reach zero and are dropped.
USAGE_HALVE_MIN = 10_000
USAGE_EVENTS_PER_NODE = 10
USAGE_ROW_KEYS = {"label", "id", "hits", "reads", "day"}
# Relations the bundled ingester derives; any other edge into a document or
# chunk is evidence or domain knowledge that refresh and compaction preserve.
INGESTER_RELS = frozenset({"CONTAINS", "HAS_CHUNK", "LINKS_TO", "MENTIONS_ENV"})
MENTION_LABELS = ("WebResource", "EnvironmentVariable")
# Compaction: what a read keeps protected, and how fast an unused item cools.
RECENT_READ_DAYS = 30
USAGE_DECAY_DAYS = 90
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
BLOB_PREFIX = "blobs/"
BLOB_NAME = re.compile(r"^[0-9a-f]{64}$")
LARGE_BLOB_BYTES = 100 * 1024 * 1024


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise GraphValidationError(f"duplicate JSON key {key!r}")
            result[key] = item
        return result

    def invalid(value):
        raise GraphValidationError(f"non-finite JSON number {value}")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


# A term found in a path or heading is scarcer than the same term in a body, and it
# says what a file is *for* rather than what it happens to mention. Weighting it
# above body evidence is what stops a huge changelog or translation file from
# burying the one module that actually implements the thing being asked about.
PATH_WEIGHT = 1.5
# Below this share of a question's information mass, a hit is noise rather than evidence.
MIN_SCORE = 0.4

STOP_WORDS = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "being",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "each",
    "find",
    "for",
    "from",
    "get",
    "give",
    "had",
    "has",
    "have",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "list",
    "make",
    "many",
    "me",
    "much",
    "must",
    "my",
    "no",
    "not",
    "of",
    "on",
    "one",
    "or",
    "our",
    "out",
    "over",
    "should",
    "show",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "up",
    "use",
    "used",
    "using",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
}


# Above this share of the graph, a bulk rebuild of the RDF index is cheaper than
# applying the change record by record. Measured on a mixed prose and code corpus.
RDF_DELTA_RATIO = 4
PAGE_SIZE = 16384


class GraphValidationError(ValueError):
    """Neutral graph violates the interchange contract."""


class GraphStateError(GraphValidationError):
    """The two stores of a graph disagree in a way that would lose data if opened."""


# The RDF index records which SQLite revision it reflects, in a named graph that
# default-graph queries never see. SQLite is authoritative and always commits
# first, so an index stamped ahead of SQLite means SQLite lost committed writes.
STATE_GRAPH = ox.NamedNode("urn:graph-skill-db:state")
STATE_REVISION = ox.NamedNode("urn:graph-skill-db:revision")


def usage_day() -> int:
    """Days since 1970-01-01 UTC; a counter row stores the last day it was used."""
    return int(time.time() // 86400)


def day_iso(day: int | None) -> str | None:
    return None if day is None else (date(1970, 1, 1) + timedelta(days=day)).isoformat()


def usage_key(label: str, identifier: str) -> int:
    """64-bit key of a node in the usage table; collisions are negligible
    (~3e-8 at a million nodes) and would only merge two approximate counters."""
    digest = hashlib.blake2b(f"{label}\0{identifier}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def graph_home() -> Path:
    return Path(os.environ.get("KG_HOME", ".kg"))


def _blob_candidates(relative: str, root: Path, source=None):
    """A record's path is relative to one ingest root, with or without its name.

    A root may also be a parent holding several sources, each in a directory
    named after its source.
    """
    yield root / relative
    head, _, tail = relative.partition("/")
    if tail and head == root.name:
        yield root / tail
    if isinstance(source, str) and source:
        yield root / source / relative


# Properties that record accumulated provenance rather than a single fact.
# Reading the same corpus from another checkout name adds a root; it does not
# contradict the earlier one, so these merge as sorted string sets.
SET_PROPERTIES = {("node", "Source", "roots")}


def _merge_set_prop(kind, record, prop, current, value):
    """Union for set-valued provenance props; None means normal conflict rules."""
    if (kind, record.get("label"), prop) not in SET_PROPERTIES:
        return None
    lists = [v for v in (current, value) if v is not None]
    if not all(
        isinstance(v, list) and all(isinstance(i, str) for i in v) for v in lists
    ):
        return None
    return sorted({item for v in lists for item in v})


def resolve_blobs(
    nodes,
    roots,
    large_bytes: int = LARGE_BLOB_BYTES,
    include_large: bool = False,
    stores=(),
) -> dict:
    """Locate the raw files behind Artifact records under the given roots.

    Matching is content addressed: a candidate path only counts when its
    sha256 equals the digest recorded at ingest, so a file edited since then
    is reported rather than silently exported as the original evidence.
    Content-addressed stores (a graph's own blob store, an import --blob-dir)
    are consulted first by digest. Artifacts larger than large_bytes are
    deferred until explicitly included.
    """
    roots = [Path(root) for root in roots]
    stores = [Path(store) for store in stores]
    blobs: dict[str, Path] = {}
    missing, changed, oversized = [], [], []
    for node in nodes:
        if node.get("label") != "Artifact":
            continue
        props = node.get("props") or {}
        digest, relative = props.get("sha256"), props.get("path")
        if not isinstance(digest, str) or not BLOB_NAME.match(digest):
            continue
        if not isinstance(relative, str) or not relative or digest in blobs:
            continue
        state, resolved = "missing", None
        for store in stores:
            candidate = store / digest[:2] / digest
            if not candidate.is_file() or candidate.is_symlink():
                continue
            size = candidate.stat().st_size
            if size > large_bytes and not include_large:
                state, resolved = "oversized", size
                break
            if stream_sha256(candidate) == digest:
                state, resolved = "found", candidate
                break
        for root in roots if state == "missing" else ():
            for candidate in _blob_candidates(relative, root, props.get("source")):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                size = candidate.stat().st_size
                if size > large_bytes and not include_large:
                    state, resolved = "oversized", size
                    break
                if stream_sha256(candidate) != digest:
                    state = "changed" if state == "missing" else state
                    continue
                state, resolved = "found", candidate
                break
            if state in ("found", "oversized"):
                break
        if state == "found":
            blobs[digest] = resolved
        elif state == "oversized":
            oversized.append({"path": relative, "size_bytes": resolved})
        elif state == "changed":
            changed.append(relative)
        else:
            missing.append(relative)
    return {
        "blobs": blobs,
        "missing": sorted(missing),
        "changed": sorted(changed),
        "oversized": sorted(oversized, key=lambda item: item["path"]),
    }


def skipped_blobs(manifest: dict) -> dict:
    available = manifest.get("blobs") or {}
    return {"extracted": 0, "bytes": 0, "available": len(available)}


def _copy_verified(stream, target_file, digest: str, block: int = 1 << 20) -> int:
    """Copy stream to target_file while hashing; raise if the digest differs."""
    hasher = hashlib.sha256()
    size = 0
    for piece in iter(lambda: stream.read(block), b""):
        hasher.update(piece)
        target_file.write(piece)
        size += len(piece)
    if hasher.hexdigest() != digest:
        raise GraphValidationError(f"blobs/{digest}: blob checksum mismatch")
    return size


def extract_blobs(package: str | Path, blob_dir: str | Path) -> dict:
    """Unpack the package's object store into a sharded content-addressed tree.

    Every member is verified against its own name before it lands on disk, so a
    tampered or truncated archive cannot install bytes under a trusted digest.
    Names come from the digest, never from the archive, so no member can escape
    the target directory. Members are streamed, so one large blob never has to
    fit in memory. Re-extracting is idempotent: `extracted` counts every blob
    now in the tree, `present` those that were already there before.
    """
    blob_dir = Path(blob_dir)
    written, total, present = 0, 0, 0
    with zipfile.ZipFile(package) as archive:
        names = [n for n in archive.namelist() if n.startswith(BLOB_PREFIX)]
        for name in sorted(names):
            digest = name[len(BLOB_PREFIX) :]
            if not BLOB_NAME.match(digest):
                raise GraphValidationError(f"{name}: not a sha256 blob name")
            target = blob_dir / digest[:2] / digest
            present += target.is_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=digest + ".", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as out, archive.open(name) as stream:
                    total += _copy_verified(stream, out, digest)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            written += 1
    return {
        "extracted": written,
        "bytes": total,
        "present": present,
        "dir": str(blob_dir),
    }


def _short(value, width: int = 80) -> str:
    text = canonical(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def conflict_message(conflicts, shown: int = 20) -> str:
    """One readable error for every conflicting field in a rejected batch."""
    lines = [
        (
            f"{len(conflicts)} property conflict{'s' if len(conflicts) != 1 else ''}; "
            "nothing was written. Use versioned IDs or separate assertion nodes, or "
            "explicitly choose on_conflict='incoming' for an intended correction:"
        )
    ]
    for kind, key, prop, current, incoming in conflicts[:shown]:
        lines.append(
            f"  {kind} {key} field {prop!r}: stored {_short(current)}, "
            f"incoming {_short(incoming)}"
        )
    if len(conflicts) > shown:
        lines.append(f"  ... and {len(conflicts) - shown} more")
    return "\n".join(lines)


def stream_sha256(path: Path, block: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_records(path: str | Path):
    """Read a JSON array or newline-delimited JSON records."""
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        yield strict_json(line)
                    except json.JSONDecodeError as exc:
                        raise GraphValidationError(
                            f"{path}:{line_number}: {exc}"
                        ) from exc
        return
    value = strict_json(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise GraphValidationError(f"{path}: expected a JSON array or .jsonl file")
    yield from value


def write_records(path: str | Path, records, jsonl: bool | None = None) -> int:
    """Write neutral records. JSONL is chosen from the suffix unless forced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if jsonl is None:
        jsonl = path.suffix.lower() == ".jsonl"
    count = 0
    if jsonl:
        with path.open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(
                    json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
                )
                count += 1
    else:
        records = list(records)
        count = len(records)
        path.write_text(
            json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return count


def _check_name(kind: str, value) -> str:
    if (
        not isinstance(value, str)
        or not SAFE_NAME.fullmatch(value)
        or value.startswith("_")
    ):
        raise GraphValidationError(
            f"invalid {kind} {value!r}; use letters, digits, and underscores"
        )
    return value


def _check_props(owner: str, props) -> dict:
    if props is None:
        return {}
    if not isinstance(props, dict):
        raise GraphValidationError(f"{owner}: props must be an object")
    for key in props:
        _check_name("property", key)
        if key == "id":
            raise GraphValidationError(
                f"{owner}: 'id' is reserved; rename the property"
            )

    def check(value):
        if value is None or type(value) in (str, bool, int):
            if isinstance(value, str):
                value.encode("utf-8")
            return
        if type(value) is float and math.isfinite(value):
            return
        if isinstance(value, list):
            for item in value:
                check(item)
            return
        if isinstance(value, dict) and all(isinstance(k, str) for k in value):
            for key, item in value.items():
                check(key)
                check(item)
            return
        raise GraphValidationError(
            f"{owner}: properties must contain finite JSON values"
        )

    try:
        check(props)
    except (UnicodeError, RecursionError) as exc:
        raise GraphValidationError(f"{owner}: invalid JSON value") from exc
    return props


def validate_graph(nodes, edges, resolve=None) -> dict:
    """Validate names, duplicates, properties, and every edge endpoint.

    `resolve` lets a merge accept endpoints that already live in the store, so a
    later batch can attach to facts an earlier batch loaded.
    """
    known: dict[tuple[str, str], str] = {}
    node_count = 0
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise GraphValidationError(f"node {index}: expected an object")
        label = _check_name("label", node.get("label"))
        identifier = node.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise GraphValidationError(f"node {index}: id must be a non-empty string")
        props = _check_props(f"node {label}:{identifier}", node.get("props"))
        if set(node) - {"label", "id", "props"}:
            raise GraphValidationError(
                f"node {index}: unknown fields; put metadata in props"
            )
        identifier.encode("utf-8")
        if label == "Chunk":
            for field in (
                "text",
                "path",
                "heading",
                "source",
                "repository",
                "document_id",
            ):
                if field in props and not isinstance(props[field], str):
                    raise GraphValidationError(
                        f"Chunk {identifier}: {field} must be a string"
                    )
            for field in ("start_line", "end_line"):
                if props.get(field) is not None and (
                    type(props[field]) is not int or props[field] < 0
                ):
                    raise GraphValidationError(
                        f"Chunk {identifier}: {field} must be a nonnegative integer"
                    )
            if (
                props.get("start_line") is not None
                and props.get("end_line") is not None
                and props["end_line"] < props["start_line"]
            ):
                raise GraphValidationError(f"Chunk {identifier}: inverted line range")
        digest = json.dumps(
            props, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        key = (label, identifier)
        if key in known and known[key] != digest:
            raise GraphValidationError(
                f"conflicting duplicate node {label}:{identifier}"
            )
        known[key] = digest
        node_count += 1

    edge_count = 0
    resolved = 0
    edge_seen = {}
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise GraphValidationError(f"edge {index}: expected an object")
        rel = _check_name("relation", edge.get("rel"))
        src_label = _check_name("label", edge.get("src_label"))
        dst_label = _check_name("label", edge.get("dst_label"))
        if set(edge) - {"rel", "src_label", "src", "dst_label", "dst", "props"}:
            raise GraphValidationError(
                f"edge {index}: unknown fields; put metadata in props"
            )
        for field in ("src", "dst"):
            if not isinstance(edge.get(field), str) or not edge[field]:
                raise GraphValidationError(
                    f"edge {index}: {field} must be a non-empty string"
                )
        identity = tuple(
            edge[k] for k in ("rel", "src_label", "src", "dst_label", "dst")
        )
        digest = canonical(_check_props(f"edge {index}", edge.get("props")))
        if identity in edge_seen and edge_seen[identity] != digest:
            raise GraphValidationError(f"conflicting duplicate edge {identity}")
        edge_seen[identity] = digest
        _check_props(f"edge {rel}", edge.get("props"))
        for role, endpoint in (
            ("source", (src_label, edge.get("src"))),
            ("destination", (dst_label, edge.get("dst"))),
        ):
            if endpoint in known:
                continue
            if (
                resolve is not None
                and isinstance(endpoint[1], str)
                and resolve(*endpoint)
            ):
                resolved += 1
                continue
            raise GraphValidationError(
                f"edge {index} {rel}: missing endpoint {role} {endpoint[0]}:{endpoint[1]}"
            )
        edge_count += 1
    result = {"nodes": node_count, "unique_nodes": len(known), "edges": edge_count}
    if resolve is not None:
        result["endpoints_resolved_in_store"] = resolved
    return result


def literal(value):
    if value is None or isinstance(value, (list, dict)):
        return ox.Literal(canonical(value), datatype=JSON_TYPE)
    if isinstance(value, bool):
        return ox.Literal(
            "true" if value else "false", datatype=ox.NamedNode(XSD + "boolean")
        )
    if isinstance(value, int):
        return ox.Literal(str(value), datatype=ox.NamedNode(XSD + "integer"))
    if isinstance(value, float):
        return ox.Literal(repr(value), datatype=ox.NamedNode(XSD + "double"))
    if isinstance(value, (list, dict)):
        return ox.Literal(json.dumps(value, sort_keys=True, ensure_ascii=False))
    return ox.Literal(str(value))


def _term_value(term):
    if isinstance(term, ox.Literal) and term.datatype == JSON_TYPE:
        return strict_json(term.value)
    if term is None:
        return None
    value = term.value
    datatype = getattr(getattr(term, "datatype", None), "value", "") or ""
    if datatype.endswith("#boolean"):
        return value == "true"
    if datatype.endswith("#integer"):
        return int(value)
    if datatype.endswith("#double"):
        return float(value)
    return value


def tokenize(text: str) -> list[str]:
    """Split a question into candidate search terms, dropping question scaffolding."""
    terms: list[str] = []
    for token in WORD.findall(text or ""):
        token = token.strip(".:-")
        if len(token) < 2 or token.casefold() in STOP_WORDS:
            continue
        if token not in terms:
            terms.append(token)
    return terms


# Labels the bundled ingester creates. They are plentiful and mostly lexical,
# so node search leaves them out unless a caller asks for them by name.
INGEST_LABELS = frozenset(
    {"Source", "Document", "Artifact", "Chunk", "WebResource", "EnvironmentVariable"}
)
# Labels for knowledge that should outlive a session: what went wrong, what the
# user wants, what is still open. `brief` lists these first.
MEMORY_LABELS = (
    "Lesson",
    "Convention",
    "Preference",
    "Decision",
    "Pitfall",
    "SkillIssue",
    "OpenQuestion",
    "Finding",
    "Todo",
)
SUMMARY_PROPS = (
    "statement",
    "title",
    "summary",
    "problem",
    "question",
    "detail",
    "description",
    "subject",
    "note",
    "name",
)
DATE_PROPS = ("observed", "observed_at", "found", "date", "updated", "created")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
NODE_TEXT_LIMIT = 4000


def _strings(value):
    if isinstance(value, str):
        yield value[:NODE_TEXT_LIMIT]
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def node_summary(props: dict, width: int = 200) -> str:
    for name in SUMMARY_PROPS:
        value = props.get(name)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= width else text[: width - 3] + "..."
    return ""


def node_date(props: dict) -> str:
    for name in DATE_PROPS:
        value = props.get(name)
        if isinstance(value, str) and DATE.match(value):
            return value
    # Session sources are usually dated ("session-2026-09-24"); that date is
    # the best ordering hint for an undated note.
    for name in ("source", "repository"):
        found = re.search(r"\d{4}-\d{2}-\d{2}", str(props.get(name) or ""))
        if found:
            return found.group(0)
    return ""


def slug(text: str, words: int = 8) -> str:
    parts = re.findall(r"[a-z0-9]+", text.casefold())
    return "-".join(parts[:words]) or "note"


def parse_ref(ref: str) -> tuple[str, str]:
    label, sep, identifier = (ref or "").partition(":")
    if not sep or not identifier or not SAFE_NAME.match(label):
        raise GraphValidationError(f"reference {ref!r} must be LABEL:ID")
    return label, identifier


def note_records(
    label: str,
    text: str,
    identifier: str | None = None,
    source: str | None = None,
    props: dict | None = None,
    supported_by=(),
    about=(),
    links=(),
    today: str | None = None,
):
    """Neutral records for one piece of session knowledge plus its links.

    `links` holds (REL, "LABEL:ID") pairs for any other relation.
    """
    _check_name("label", label)
    if not isinstance(text, str) or not text.strip():
        raise GraphValidationError("a note needs non-empty text")
    today = today or datetime.now(timezone.utc).date().isoformat()
    identifier = identifier or f"{label.lower()}:{slug(text)}"
    node_props = {
        "statement": text.strip(),
        "observed": today,
        "source": source or f"session-{today}",
        **(props or {}),
    }
    node = {"label": label, "id": identifier, "props": node_props}
    pairs = (
        [("SUPPORTED_BY", ref) for ref in supported_by]
        + [("ABOUT", ref) for ref in about]
        + list(links)
    )
    edges = []
    for rel, ref in pairs:
        _check_name("relation", rel)
        dst_label, dst = parse_ref(ref)
        edges.append(
            {
                "rel": rel,
                "src_label": label,
                "src": identifier,
                "dst_label": dst_label,
                "dst": dst,
            }
        )
    return [node], edges


def _usage_rows(data: bytes, declared) -> list[dict]:
    """Validate the usage member: one row per node, small non-negative counters."""
    if not isinstance(declared, dict) or any(
        type(declared.get(k)) is not int or declared[k] < 0
        for k in ("rows", "since", "generation")
    ):
        raise GraphValidationError("invalid manifest usage entry")
    rows = [
        strict_json(line) for line in data.decode("utf-8").splitlines() if line.strip()
    ]
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != USAGE_ROW_KEYS
            or not all(isinstance(row[k], str) and row[k] for k in ("label", "id"))
            or any(
                type(row[k]) is not int or row[k] < 0 for k in ("hits", "reads", "day")
            )
        ):
            raise GraphValidationError(f"{USAGE_MEMBER}: invalid row {row!r}"[:200])
    if len(rows) != declared["rows"]:
        raise GraphValidationError(f"{USAGE_MEMBER}: row count mismatch")
    return rows


def diff_records(a_nodes, a_edges, b_nodes, b_edges, top: int = 20) -> dict:
    """Compare two record sets (packages or graphs) before merging them.

    Reports per-label/per-relation counts only in A, only in B, and differing;
    memory notes unique to each side or conflicting by ID; and paths that would
    have more than one current Document version after a union, the sign that
    one side refreshed a source and the other did not.
    """

    def index(nodes, edges):
        return (
            {(n["label"], n["id"]): n.get("props") or {} for n in nodes},
            {
                (e["rel"], e["src_label"], e["src"], e["dst_label"], e["dst"]): e.get(
                    "props"
                )
                or {}
                for e in edges
            },
        )

    an, ae = index(a_nodes, a_edges)
    bn, be = index(b_nodes, b_edges)

    def counts(keys):
        return dict(sorted(Counter(key[0] for key in keys).items()))

    def memory(x, y):
        found = [
            {
                "label": k[0],
                "id": k[1],
                "statement": str(p.get("statement") or "")[:160],
            }
            for k, p in sorted(x.items())
            if k[0] in MEMORY_LABELS and k not in y
        ]
        return found[:top]

    versions: dict[tuple, set] = {}
    for side in (an, bn):
        for key, props in side.items():
            if key[0] == "Document" and not props.get("outdated"):
                versions.setdefault(
                    (props.get("source"), props.get("path")), set()
                ).add(key[1])
    duplicated = sorted(k for k, v in versions.items() if len(v) > 1 and k[1])
    return {
        "nodes": {
            "only_a": counts(k for k in an if k not in bn),
            "only_b": counts(k for k in bn if k not in an),
            "differ": counts(
                k for k in an if k in bn and canonical(an[k]) != canonical(bn[k])
            ),
        },
        "edges": {
            "only_a": counts(k for k in ae if k not in be),
            "only_b": counts(k for k in be if k not in ae),
            "differ": counts(
                k for k in ae if k in be and canonical(ae[k]) != canonical(be[k])
            ),
        },
        "memory": {
            "only_a": memory(an, bn),
            "only_b": memory(bn, an),
            "conflicting": [
                {"label": k[0], "id": k[1]}
                for k in sorted(an)
                if k[0] in MEMORY_LABELS
                and k in bn
                and canonical(an[k]) != canonical(bn[k])
            ][:top],
        },
        "current_versions": {
            "paths": len(duplicated),
            "by_source": dict(Counter(str(k[0]) for k in duplicated)),
            "examples": [
                {"source": k[0], "path": k[1], "versions": len(versions[k])}
                for k in duplicated[:top]
            ],
        },
    }


class _DryRun(Exception):
    """Raised inside a transaction to roll a preview back."""


class KG:
    def __init__(self, name: str, ns: str | None = None, create: bool = False):
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name
        ):
            raise GraphValidationError("graph name must be a simple name, not a path")
        self.name = name
        self.home = graph_home()
        self.graph_path = self.home / "rdf" / f"{name}.oxi"
        self.content_path = self.home / "content" / f"{name}.sqlite3"
        # Raw artifact bytes that arrived in an imported package. Keeping them
        # beside the records is what lets the next export carry them on; without
        # it, one import/export round trip silently dropped every blob.
        self.blob_path = self.home / "blobs" / name
        self.file = self.graph_path
        rdf_existed = self.graph_path.exists()
        if not create and not (self.graph_path.exists() or self.content_path.exists()):
            raise FileNotFoundError(f"no graph {name!r} at {self.home}")
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.content_path.parent.mkdir(parents=True, exist_ok=True)
        # Acquire the exclusive graph lock before touching SQLite.
        self.store = ox.Store(str(self.graph_path))
        self.content = None
        self.track_usage = os.environ.get("KG_USAGE", "1") != "0"
        self._usage_buffer: dict[int, list[int]] = {}
        try:
            stamp = self._state_stamp()
            if stamp is not None and not self.content_path.exists():
                raise self._state_error(None, stamp)
            self.content = sqlite3.connect(self.content_path)
            self.content.row_factory = sqlite3.Row
            self._prepare_content()
            # Keys are stored once: a rowid table with a (kind, key) primary key
            # kept every key twice, in the table and again in its index.
            self.content.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(kind,key)) WITHOUT ROWID;
            """)
            self._check_state(stamp)
            self._prepare_usage()
            self._prepare_node_index()
            saved = self._meta("namespace")
            inferred = set()
            if not saved:
                for subject, _, obj, _ in self.store.quads_for_pattern(
                    None, RDF_TYPE, None
                ):
                    match = re.fullmatch(r"(.*[:/#])([A-Za-z][A-Za-z0-9_]*)", obj.value)
                    if match and subject.value.startswith(obj.value + "/"):
                        inferred.add(match.group(1))
            if len(inferred) > 1 and not saved:
                raise GraphValidationError(
                    "ambiguous legacy namespace; export with the original skill first"
                )
            if not saved and inferred and ns is not None and ns not in inferred:
                raise GraphValidationError("namespace conflict with legacy graph")
            if not saved and self._triple_count() and not inferred:
                raise GraphValidationError(
                    "cannot identify legacy namespace; export with the original skill first"
                )
            self.ns = (
                saved
                or ns
                or next(iter(inferred), None)
                or os.environ.get("KG_NAMESPACE", DEFAULT_NS)
            )
            ox.NamedNode(self.ns)
            if any(char.isspace() or char in "<>" for char in self.ns):
                raise GraphValidationError("invalid namespace")
            if saved and ns is not None and saved != ns:
                raise GraphValidationError(
                    f"namespace conflict: this graph uses {saved!r}"
                )
            if not self._meta("revision"):
                nodes, edges = self._legacy_records()
                validate_graph(nodes, edges)
                with self.content:
                    self._set_meta("namespace", self.ns)
                    for kind, records in (("node", nodes), ("edge", edges)):
                        for record in records:
                            self._put(kind, record)
                            if kind == "node" and record["label"] == "Chunk":
                                self._write_chunk(record)
                    self._set_meta("revision", "1")
            migrated = self._migrate_chunk_text() | self._migrate_records_layout()
            if (
                migrated
                or self.content.execute("PRAGMA page_size").fetchone()[0] != PAGE_SIZE
            ):
                self.content.execute(f"PRAGMA page_size={PAGE_SIZE}")
                self.content.execute("VACUUM")
            if not rdf_existed:
                with self.content:
                    self._set_meta("rdf_revision", "0")
            self._ensure_rdf()
        except BaseException:
            self.close()
            raise

    def close(self):
        content = getattr(self, "content", None)
        if content is not None:
            try:
                self._flush_usage()
            except sqlite3.Error as exc:
                # Counters are approximate by design; never fail a command on them.
                print(f"warning: usage counters not saved: {exc}", file=sys.stderr)
            content.close()
            self.content = None
        if getattr(self, "store", None) is not None:
            self.store.flush()
            self.store = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _meta(self, key):
        row = self.content.execute(
            "SELECT value FROM metadata WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        self.content.execute(
            "INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, str(value))
        )

    @staticmethod
    def _key(kind, record):
        fields = (
            ("label", "id")
            if kind == "node"
            else ("rel", "src_label", "src", "dst_label", "dst")
        )
        return canonical([record[field] for field in fields])

    @staticmethod
    def _stored_form(kind, record):
        """The record as written to `records`.

        Chunk text lives once, in the `chunks` table that the full-text index
        reads. Keeping a second copy inside the record JSON roughly doubled the
        bytes a text-heavy graph needs. Every caller writes the chunk row in the
        same transaction (`_write_chunk`), and `_hydrate` puts the text back on read.
        """
        props = record.get("props") or {}
        if (
            kind == "node"
            and record["label"] == "Chunk"
            and isinstance(props.get("text"), str)
        ):
            record = {
                **record,
                "props": {k: v for k, v in props.items() if k != "text"},
            }
        return record

    def _hydrate(self, record):
        """Return a record with Chunk text restored from the `chunks` table."""
        if record.get("label") != "Chunk":
            return record
        props = record.get("props") or {}
        if "text" in props:
            # Written before text was stored once, and kept inline because the
            # two copies disagreed during migration: the record copy wins.
            return record
        row = self.content.execute(
            "SELECT text FROM chunks WHERE node_iri=?",
            (self.node_iri("Chunk", record["id"]).value,),
        ).fetchone()
        if row is not None:
            record["props"] = {**props, "text": row[0]}
        return record

    # ------------------------------------------------------------------ usage
    def _prepare_usage(self) -> None:
        """One row per used node: hits (returned by a command) and reads (opened).

        Never-used nodes have no row; `usage_since` says since when that holds.
        """
        with self.content:
            self.content.execute(
                "CREATE TABLE IF NOT EXISTS usage(h INTEGER PRIMARY KEY, "
                "hits INTEGER NOT NULL DEFAULT 0, reads INTEGER NOT NULL DEFAULT 0, "
                "day INTEGER NOT NULL)"
            )
            if self._meta("usage_since") is None:
                self._set_meta("usage_since", usage_day())

    def record_use(self, hits=(), reads=()) -> None:
        """Count (label, id) pairs a caller returned (hits) or opened (reads).

        Counts are buffered and written in one transaction when the graph closes,
        so a command pays one small write however many items it touched.
        """
        if not self.track_usage:
            return
        for column, items in ((0, hits), (1, reads)):
            for label, identifier in items:
                row = self._usage_buffer.setdefault(
                    usage_key(label, identifier), [0, 0]
                )
                row[column] += 1

    def _usage_halve_at(self) -> int:
        nodes = self.content.execute(
            "SELECT COUNT(*) FROM records WHERE kind='node'"
        ).fetchone()[0]
        return max(USAGE_HALVE_MIN, USAGE_EVENTS_PER_NODE * nodes)

    def _halve_usage(self, steps: int = 1) -> None:
        self.content.execute(
            "UPDATE usage SET hits = hits >> ?, reads = reads >> ?", (steps, steps)
        )
        self.content.execute("DELETE FROM usage WHERE hits = 0 AND reads = 0")

    def _flush_usage(self) -> None:
        if not self._usage_buffer:
            return
        day = usage_day()
        rows = [
            (key, hits, reads, day) for key, (hits, reads) in self._usage_buffer.items()
        ]
        events = sum(hits + reads for _, hits, reads, _ in rows)
        with self.content:
            self.content.executemany(
                "INSERT INTO usage(h, hits, reads, day) VALUES (?,?,?,?) "
                "ON CONFLICT(h) DO UPDATE SET hits = hits + excluded.hits, "
                "reads = reads + excluded.reads, day = excluded.day",
                rows,
            )
            total = int(self._meta("usage_events") or 0) + events
            # The threshold is never below USAGE_HALVE_MIN, so the node count it
            # needs is only taken once that floor is reached.
            if total >= USAGE_HALVE_MIN and total >= self._usage_halve_at():
                self._halve_usage()
                generation = int(self._meta("usage_generation") or 0) + 1
                self._set_meta("usage_generation", generation)
                total = 0
            self._set_meta("usage_events", total)
        self._usage_buffer.clear()

    def _node_keys(self) -> dict[int, tuple[str, str]]:
        keys = {}
        for (key,) in self.content.execute("SELECT key FROM records WHERE kind='node'"):
            label, identifier = strict_json(key)
            keys[usage_key(label, identifier)] = (label, identifier)
        return keys

    def usage_rows(self) -> list[dict]:
        """Counters of existing nodes as portable rows, sorted by label and ID."""
        keys = self._node_keys()
        rows = []
        for key, hits, reads, day in self.content.execute(
            "SELECT h, hits, reads, day FROM usage"
        ):
            if key in keys:
                label, identifier = keys[key]
                rows.append(
                    {
                        "label": label,
                        "id": identifier,
                        "hits": hits,
                        "reads": reads,
                        "day": day,
                    }
                )
        return sorted(rows, key=lambda row: (row["label"], row["id"]))

    def _usage_meta(self) -> dict:
        return {
            "since": int(self._meta("usage_since")),
            "generation": int(self._meta("usage_generation") or 0),
        }

    def _merge_usage(self, rows, incoming: dict, replace: bool = False) -> int:
        """Merge counters by maximum, so re-importing a package never double counts.

        Two sessions that branched from one package can each lose the other's
        increments; that undercount is the price of idempotent merges.
        """
        with self.content:
            if replace:
                self.content.execute("DELETE FROM usage")
                self._set_meta("usage_generation", incoming["generation"])
                self._set_meta("usage_events", 0)
                self._set_meta("usage_since", incoming["since"])
            local = self._usage_meta()
            if incoming["generation"] > local["generation"]:
                self._halve_usage(incoming["generation"] - local["generation"])
                self._set_meta("usage_generation", incoming["generation"])
                local["generation"] = incoming["generation"]
            shift = local["generation"] - incoming["generation"]
            merged = []
            for row in rows:
                hits, reads = row["hits"] >> shift, row["reads"] >> shift
                if (hits or reads) and self.exists(row["label"], row["id"]):
                    merged.append(
                        (usage_key(row["label"], row["id"]), hits, reads, row["day"])
                    )
            self.content.executemany(
                "INSERT INTO usage(h, hits, reads, day) VALUES (?,?,?,?) "
                "ON CONFLICT(h) DO UPDATE SET hits = max(hits, excluded.hits), "
                "reads = max(reads, excluded.reads), day = max(day, excluded.day)",
                merged,
            )
            self._set_meta("usage_since", min(local["since"], incoming["since"]))
        return len(merged)

    def usage_report(self, top: int = 10) -> dict:
        """Where use concentrates and which stored text nobody touched.

        Reading the report is not use: it records nothing.
        """
        counters = {
            key: (hits, reads, day)
            for key, hits, reads, day in self.content.execute(
                "SELECT h, hits, reads, day FROM usage"
            )
        }
        keys = self._node_keys()
        labels: dict[str, dict] = {}
        for key, (label, _) in keys.items():
            entry = labels.setdefault(
                label, {"nodes": 0, "used": 0, "hits": 0, "reads": 0}
            )
            entry["nodes"] += 1
            if key in counters:
                entry["used"] += 1
                entry["hits"] += counters[key][0]
                entry["reads"] += counters[key][1]
        text = {"bytes": 0, "used_bytes": 0, "cold_bytes": 0}
        sources: dict[str, dict] = {}
        documents: dict[str, dict] = {}
        for identifier, repository, path, document, size in self.content.execute(
            "SELECT id, repository, path, document_id, length(CAST(text AS BLOB)) "
            "FROM chunks"
        ):
            used = usage_key("Chunk", identifier) in counters
            text["bytes"] += size
            text["used_bytes" if used else "cold_bytes"] += size
            source = sources.setdefault(
                repository,
                {
                    "source": repository,
                    "chunks": 0,
                    "used_chunks": 0,
                    "text_bytes": 0,
                    "cold_bytes": 0,
                },
            )
            source["chunks"] += 1
            source["used_chunks"] += used
            source["text_bytes"] += size
            source["cold_bytes"] += 0 if used else size
            group = documents.setdefault(
                document,
                {
                    "path": path,
                    "source": repository,
                    "chunks": 0,
                    "text_bytes": 0,
                    "used": False,
                },
            )
            group["chunks"] += 1
            group["text_bytes"] += size
            group["used"] |= used
        hot = sorted(
            (
                {
                    "label": keys[key][0],
                    "id": keys[key][1],
                    "hits": hits,
                    "reads": reads,
                    "last_used": day_iso(day),
                }
                for key, (hits, reads, day) in counters.items()
                if key in keys
            ),
            key=lambda row: (
                -(4 * row["reads"] + row["hits"]),
                row["label"],
                row["id"],
            ),
        )
        cold = sorted(
            (dict(group) for group in documents.values() if not group["used"]),
            key=lambda group: (-group["text_bytes"], group["path"]),
        )
        for group in cold:
            group.pop("used")
        meta = self._usage_meta()
        return {
            "tracking": {
                "enabled": self.track_usage,
                "since": day_iso(meta["since"]),
                "generation": meta["generation"],
                "events_since_halving": int(self._meta("usage_events") or 0),
                "halve_at": self._usage_halve_at(),
                "rows": len(counters),
            },
            "labels": dict(sorted(labels.items())),
            "chunk_text": text,
            "sources": sorted(
                sources.values(), key=lambda s: (-s["cold_bytes"], s["source"])
            )[:top],
            "cold_documents": cold[:top],
            "hot": hot[:top],
        }

    # ----------------------------------------------------------------- refresh
    def refresh_source(
        self,
        source: str,
        nodes,
        edges=(),
        apply: bool = False,
        superseded_by: str | None = None,
        bookkeeping_rels=(),
        on_conflict: str = "incoming",
    ) -> dict:
        """Bring one source up to a fresh ingest without losing cited evidence.

        `load --source` drops everything the source owns, including versions
        that lessons and findings cite. Here a Document or Artifact the fresh
        batch no longer contains is stale: with its chunks it is deleted when no
        evidence edge touches it, and otherwise kept and marked `outdated`. A
        version that reappears loses its marks. Ingester relations and the given
        `bookkeeping_rels` (such as a snapshot's INCLUDES) are not evidence.
        Everything commits in one transaction; without `apply` it is a preview.
        The batch belongs to this one source, so by default its values win over
        stored ones (a newer ingester can move the locators of unchanged text);
        properties the batch does not supply, such as notes' marks, are kept.
        """
        if not isinstance(source, str) or not source:
            raise GraphValidationError("source must be a non-empty string")
        nodes, edges = list(nodes), list(edges)
        for record in nodes + edges:
            owners = self._owners(record)
            if (owners and owners != {source}) or (
                record.get("label") == "Source" and record["id"] != source
            ):
                raise GraphValidationError("refresh batch contains another source")
        ignore = INGESTER_RELS | set(bookkeeping_rels)
        today = day_iso(usage_day())
        stored = {
            (n["label"], n["id"]): n.get("props") or {}
            for n in self._records_raw("node")
        }
        stored_edges = list(self._records_raw("edge"))
        incident: dict[tuple, list] = {}
        chunks_of: dict[tuple, list] = {}
        contained = set()
        for edge in stored_edges:
            src, dst = (
                (edge["src_label"], edge["src"]),
                (edge["dst_label"], edge["dst"]),
            )
            incident.setdefault(src, []).append(edge)
            incident.setdefault(dst, []).append(edge)
            if edge["rel"] == "HAS_CHUNK":
                chunks_of.setdefault(src, []).append(dst)
            if edge["rel"] == "CONTAINS" and src == ("Source", source):
                contained.add(dst)
        versions = ("Document", "Artifact")
        current = {
            node
            for node, props in stored.items()
            if node[0] in versions
            and (node in contained or self._owners({"props": props}) == {source})
            and not props.get("outdated")
        }
        fresh = {(n["label"], n["id"]) for n in nodes if n["label"] in versions}
        stale = sorted(current - fresh)
        # A record the batch contains again loses its outdated marks.
        revived = sorted(
            (n["label"], n["id"])
            for n in nodes
            if stored.get((n["label"], n["id"]), {}).get("outdated")
        )
        # An unchanged document keeps its ID (it is derived from the file's
        # bytes), but re-chunking it (another extractor or chunk size) yields new
        # chunk IDs: the chunks the batch no longer attaches to it are stale too.
        fresh_chunks: dict[tuple, set] = {}
        for edge in edges:
            if edge["rel"] == "HAS_CHUNK":
                fresh_chunks.setdefault((edge["src_label"], edge["src"]), set()).add(
                    (edge["dst_label"], edge["dst"])
                )
        stale_chunks = sorted(
            chunk
            for doc in fresh & current
            for chunk in chunks_of.get(doc, [])
            if chunk not in fresh_chunks.get(doc, set())
            and not stored.get(chunk, {}).get("outdated")
        )
        marked, doomed = [], set()
        for group in [[doc, *chunks_of.get(doc, [])] for doc in stale] + [
            [chunk] for chunk in stale_chunks
        ]:
            if any(e["rel"] not in ignore for m in group for e in incident.get(m, [])):
                marked.extend(group)
            else:
                doomed.update(group)
        orphans = {
            end
            for member in doomed
            for edge in incident.get(member, [])
            for end in (
                (edge["src_label"], edge["src"]),
                (edge["dst_label"], edge["dst"]),
            )
            if end[0] in MENTION_LABELS
            and all(
                (e["src_label"], e["src"]) in doomed
                or (e["dst_label"], e["dst"]) in doomed
                for e in incident.get(end, [])
            )
        }
        doomed |= orphans
        evidence_before = sum(e["rel"] not in ignore for e in stored_edges)
        marks = {"outdated": True, "outdated_since": today}
        if superseded_by is not None:
            marks["superseded_by"] = superseded_by

        def prepare():
            removed, changed = [], []
            for member in sorted(doomed):
                record = self._record("node", list(member))
                if record:
                    self._unput("node", record)
                    removed.append(("node", record))
            for edge in stored_edges:
                ends = {
                    (edge["src_label"], edge["src"]),
                    (edge["dst_label"], edge["dst"]),
                }
                if ends & doomed:
                    self._unput("edge", edge)
                    removed.append(("edge", edge))
            rewrites = [(member, True) for member in marked] + [
                (member, False) for member in revived
            ]
            for member, mark in rewrites:
                previous = self._record("node", list(member))
                if previous is None or (mark and previous["props"].get("outdated")):
                    continue  # keep the date it first went out of date
                props = {
                    k: v
                    for k, v in previous["props"].items()
                    if mark or k not in ("outdated", "outdated_since", "superseded_by")
                }
                if mark:
                    props.update(marks)
                current_record = {**previous, "props": props}
                if canonical(current_record) != canonical(previous):
                    self._put("node", current_record)
                    changed.append(("node", previous, current_record))
            return removed, changed

        result = self.load(
            nodes, edges, dry_run=not apply, on_conflict=on_conflict, _prepare=prepare
        )
        stored_keys = {self._key("edge", e) for e in stored_edges}
        evidence_after = (
            sum(e["rel"] not in ignore for e in self._records_raw("edge"))
            if apply
            else evidence_before
            + sum(
                e["rel"] not in ignore and self._key("edge", e) not in stored_keys
                for e in edges
            )
        )
        return {
            "dry_run": not apply,
            "source": source,
            "fresh": len(fresh),
            "added": len(fresh - set(stored)),
            "unchanged": len(fresh & current),
            "stale": len(stale),
            "stale_chunks": len(stale_chunks),
            "deleted_documents": sum(1 for d in doomed if d[0] in versions),
            "deleted_nodes": len(doomed),
            "marked_outdated": sum(1 for m in marked if m[0] in versions),
            "revived": sum(1 for r in revived if r[0] in versions),
            "orphans_removed": len(orphans),
            "evidence_edges_before": evidence_before,
            (
                "evidence_edges_after" if apply else "projected_evidence_edges_after"
            ): evidence_after,
            "created": result.get("created"),
            "updated": result.get("updated"),
            **({"conflicts": result["conflicts"]} if result.get("conflicts") else {}),
        }

    # -------------------------------------------------------------- compaction
    def store_bytes(self) -> int:
        size = sum(p.stat().st_size for p in self.graph_path.rglob("*") if p.is_file())
        return size + sum(
            p.stat().st_size
            for p in self.content_path.parent.glob(self.content_path.name + "*")
            if p.is_file()
        )

    def _text_overhead(self) -> float:
        """Stored bytes per byte of chunk text: table plus its full-text indexes."""
        text = self.content.execute(
            "SELECT COALESCE(SUM(length(CAST(text AS BLOB))), 0) FROM chunks"
        ).fetchone()[0]
        try:
            stored = self.content.execute(
                "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = 'chunks' "
                "OR name LIKE 'chunks_fts%' OR name LIKE 'locations_fts%'"
            ).fetchone()[0]
        except sqlite3.Error:
            stored = 0
        return stored / text if text and stored else 1.3

    def _keep_value(self, counters, key, since, today) -> float:
        hits, reads, day = counters.get(key, (0, 0, since))
        return (1 + 4 * reads + hits) * math.exp(-(today - day) / USAGE_DECAY_DAYS)

    def _compaction_plan(self, today: int) -> dict:
        nodes = {
            (n["label"], n["id"]): n.get("props") or {}
            for n in self._records_raw("node")
        }
        edges = list(self._records_raw("edge"))
        counters = {
            key: (hits, reads, day)
            for key, hits, reads, day in self.content.execute(
                "SELECT h, hits, reads, day FROM usage"
            )
        }
        since = int(self._meta("usage_since") or today)
        memory = {node for node in nodes if node[0] in MEMORY_LABELS}
        evidence = set()
        chunks_of: dict[tuple, list] = {}
        incident: dict[tuple, list] = {}
        for edge in edges:
            src, dst = (
                (edge["src_label"], edge["src"]),
                (edge["dst_label"], edge["dst"]),
            )
            incident.setdefault(src, []).append(edge)
            incident.setdefault(dst, []).append(edge)
            if edge["rel"] == "HAS_CHUNK" and src[0] == "Document":
                chunks_of.setdefault(src, []).append(dst)
            if edge["rel"] not in INGESTER_RELS:
                evidence |= {
                    end for end in (src, dst) if end[0] in ("Chunk", "Document")
                }
        recent = {
            node
            for node in nodes
            if node[0] in ("Chunk", "Document")
            and counters.get(usage_key(*node), (0, 0, 0))[1]
            and counters[usage_key(*node)][2] >= today - RECENT_READ_DAYS
        }
        protected = memory | evidence | recent
        sizes = {
            row[0]: row[1]
            for row in self.content.execute(
                "SELECT id, length(CAST(text AS BLOB)) FROM chunks"
            )
        }
        overhead = self._text_overhead()
        outdated = {
            node
            for node, props in nodes.items()
            if node[0] == "Document" and props.get("outdated")
        }
        in_outdated = {c for doc in outdated for c in chunks_of.get(doc, [])}
        level1 = []
        for (label, identifier), props in nodes.items():
            node = (label, identifier)
            if label != "Chunk" or identifier not in sizes or node in protected:
                continue
            if node in in_outdated:
                continue  # superseded versions are level 2's, as a whole
            saving = sizes[identifier] * overhead
            value = self._keep_value(counters, usage_key(*node), since, today)
            level1.append(
                {
                    "node": node,
                    "bytes": sizes[identifier],
                    "saving": saving,
                    "rank": value / max(saving, 1),
                    "path": props.get("path"),
                    "source": props.get("source") or props.get("repository"),
                }
            )
        level1.sort(key=lambda item: (item["rank"], item["node"]))
        level2 = []
        for doc in sorted(outdated):
            group = [doc, *chunks_of.get(doc, [])]
            if any(member in protected for member in group):
                continue
            text = sum(
                sizes.get(member[1], 0) for member in group if member[0] == "Chunk"
            )
            saving = text * overhead + sum(
                len(canonical(nodes.get(m, {}))) for m in group
            )
            value = sum(
                self._keep_value(counters, usage_key(*m), since, today) for m in group
            )
            level2.append(
                {
                    "group": group,
                    "saving": saving,
                    "rank": value / max(saving, 1),
                    "path": nodes[doc].get("path"),
                }
            )
        level2.sort(key=lambda item: (item["rank"], item["group"][0]))
        return {
            "level1": level1,
            "level2": level2,
            "incident": incident,
            "protected": {
                "memory": len(memory),
                "evidence": len(evidence - memory),
                "recent_reads": len(recent - evidence - memory),
            },
        }

    def _records_raw(self, kind):
        """Records as stored (Chunk text not hydrated): cheap full scans."""
        for row in self.content.execute(
            "SELECT value FROM records WHERE kind=? ORDER BY key", (kind,)
        ):
            yield strict_json(row[0])

    def _lossless_compaction(self) -> None:
        with self.content:
            for table in ("chunks_fts", "node_fts"):
                self.content.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
        self.content.execute("VACUUM")

    def _evict_text(self, chosen, today: int) -> None:
        base = self._meta("revision")
        changed = []
        with self.content:
            for item in chosen:
                label, identifier = item["node"]
                previous = self._record("node", [label, identifier])
                props = {k: v for k, v in previous["props"].items() if k != "text"}
                props["evicted"] = day_iso(today)
                current = {**previous, "props": props}
                self._put("node", current)
                self.content.execute(
                    "DELETE FROM chunks WHERE node_iri=?",
                    (self.node_iri(label, identifier).value,),
                )
                changed.append(("node", previous, current))
            self._set_meta("revision", int(base) + 1)
        self._apply_rdf_delta(base, [], changed)

    def compact(
        self,
        target_bytes: int | None = None,
        target_percent: float | None = None,
        apply: bool = False,
        checkpoint: bool | str | Path = True,
        lossless: bool = True,
        lossless_only: bool = False,
    ) -> dict:
        """Reduce stored size by at least a target; only ever on explicit request.

        Level 0 is lossless (VACUUM, full-text index merge). Level 1 evicts the
        text of the coldest unprotected chunks and keeps a stub with digest and
        locator, so re-ingesting the source restores it. Level 2 drops cold
        superseded (`outdated`) document versions nobody cites. Memory nodes,
        evidence cited by any non-ingester edge, and items read in the last
        RECENT_READ_DAYS are never touched. Items go in order of the least kept
        value per byte saved, and selection stops once the target is reached.
        Without `apply` nothing changes; with it, a checkpoint package is
        exported before the first lossy step unless `checkpoint` is False.
        """
        if (target_bytes is None) == (target_percent is None):
            raise GraphValidationError(
                "compact needs exactly one target: target_bytes or target_percent"
            )
        if (target_bytes is not None and target_bytes <= 0) or (
            target_percent is not None and not 0 < target_percent <= 100
        ):
            raise GraphValidationError(
                "compact target must be positive (percent <= 100)"
            )
        self._flush_usage()
        today = usage_day()
        before = self.store_bytes()
        goal = (
            target_bytes
            if target_bytes is not None
            else math.ceil(before * target_percent / 100)
        )
        page_size = self.content.execute("PRAGMA page_size").fetchone()[0]
        free = self.content.execute("PRAGMA freelist_count").fetchone()[0] * page_size
        levels = [
            {
                "level": 0,
                "action": "vacuum and merge full-text indexes",
                "lossless": True,
                "projected_bytes": free if lossless else 0,
            }
        ]
        remaining = goal
        if lossless:
            if apply:
                self._lossless_compaction()
                levels[0]["saved_bytes"] = before - self.store_bytes()
                remaining -= levels[0]["saved_bytes"]
            else:
                remaining -= free
        plan = None if lossless_only else self._compaction_plan(today)
        chosen1, chosen2 = [], []
        if plan:
            for item in plan["level1"]:
                if remaining <= 0:
                    break
                chosen1.append(item)
                remaining -= item["saving"]
            for item in plan["level2"]:
                if remaining <= 0:
                    break
                chosen2.append(item)
                remaining -= item["saving"]
            sources = Counter(item["source"] or "" for item in chosen1)
            levels.append(
                {
                    "level": 1,
                    "action": "evict text of cold chunks, keep stub",
                    "lossless": False,
                    "chunks": len(chosen1),
                    "text_bytes": sum(item["bytes"] for item in chosen1),
                    "projected_bytes": round(sum(item["saving"] for item in chosen1)),
                    "by_source": dict(sources.most_common(10)),
                    "examples": [
                        item["path"] or item["node"][1] for item in chosen1[:10]
                    ],
                }
            )
            levels.append(
                {
                    "level": 2,
                    "action": "drop cold superseded document versions",
                    "lossless": False,
                    "documents": len(chosen2),
                    "chunks": sum(len(item["group"]) - 1 for item in chosen2),
                    "projected_bytes": round(sum(item["saving"] for item in chosen2)),
                    "examples": [
                        item["path"] or item["group"][0][1] for item in chosen2[:10]
                    ],
                }
            )
        saved_checkpoint = None
        if apply and (chosen1 or chosen2):
            if checkpoint is not False:
                target = (
                    Path(checkpoint)
                    if not isinstance(checkpoint, bool)
                    else self.home
                    / "checkpoints"
                    / (
                        f"{self.name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
                        + PACKAGE_SUFFIX
                    )
                )
                exported = self.export_package(target)
                saved_checkpoint = {
                    "package": exported["package"],
                    "bytes": exported["bytes"],
                }
            if chosen1:
                self._evict_text(chosen1, today)
            if chosen2:
                doomed = {member for item in chosen2 for member in item["group"]}
                orphans = {
                    end
                    for member in doomed
                    for edge in plan["incident"].get(member, [])
                    for end in (
                        (edge["src_label"], edge["src"]),
                        (edge["dst_label"], edge["dst"]),
                    )
                    if end[0] in MENTION_LABELS
                    and all(
                        (e["src_label"], e["src"]) in doomed
                        or (e["dst_label"], e["dst"]) in doomed
                        for e in plan["incident"].get(end, [])
                    )
                }
                self.delete(nodes=sorted(doomed | orphans))
            self.content.execute("VACUUM")
        after = self.store_bytes() if apply else before - (goal - remaining)
        return {
            "dry_run": not apply,
            "before_bytes": before,
            ("after_bytes" if apply else "projected_after_bytes"): after,
            "target_bytes": goal,
            "target_met": before - after >= goal,
            "levels": levels,
            "protected": plan["protected"] if plan else None,
            # Few days of counters means the ranking is mostly by size: say so.
            "usage_days": today - int(self._meta("usage_since") or today),
            "checkpoint": saved_checkpoint,
        }

    # ------------------------------------------------------------ consistency
    def _state_stamp(self) -> int | None:
        for quad in self.store.quads_for_pattern(
            None, STATE_REVISION, None, STATE_GRAPH
        ):
            return int(quad.object.value)
        return None

    def _stamp_state(self, revision) -> None:
        self._clear_state_stamp(flush=False)
        self.store.add(
            ox.Quad(STATE_GRAPH, STATE_REVISION, ox.Literal(int(revision)), STATE_GRAPH)
        )

    def _clear_state_stamp(self, flush: bool = True) -> None:
        for quad in list(
            self.store.quads_for_pattern(None, STATE_REVISION, None, STATE_GRAPH)
        ):
            self.store.remove(quad)
        if flush:
            self.store.flush()

    def _state_error(self, revision, stamp) -> GraphStateError:
        return GraphStateError(
            f"graph {self.name!r}: its SQLite content (revision {revision or 'none'}) "
            f"is older than its RDF index (revision {stamp}). The content file "
            f"{self.content_path} or its write-ahead log was lost or replaced after a "
            "committed write. Nothing was changed. Move rdf/"
            f"{self.name}.oxi and content/{self.name}.sqlite3* aside and import the "
            "newest exported package; do not rebuild from the index, which has no "
            "chunk text."
        )

    def _check_state(self, stamp) -> None:
        """Refuse to open a graph whose SQLite store went backwards.

        Opening it anyway would show an empty or partial graph and, on the next
        write, clear the RDF index to match: the one place the facts survived.
        """
        revision = self._meta("revision")
        if stamp is not None and (revision is None or int(revision) < stamp):
            raise self._state_error(revision, stamp)
        if (
            stamp is None
            and revision is not None
            and self._meta("rdf_revision") == revision
            and len(self.store)
            and not self.content.execute("SELECT 1 FROM records LIMIT 1").fetchone()
        ):
            raise self._state_error(revision, "populated, unstamped")

    def _compact_after(self, changed: int) -> None:
        """VACUUM after a write that rewrote a large share of the graph.

        Bulk inserts in key order leave pages part-filled, and deletions leave
        free pages; on the measured corpora a VACUUM after import saved ~5%.
        It is lossless and skipped for the small deltas of everyday work.
        """
        total = self.content.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        if changed >= 1000 and changed * 4 >= total:
            self.content.execute("VACUUM")

    def _triple_count(self) -> int:
        return len(self.store) - (self._state_stamp() is not None)

    def _migrate_records_layout(self) -> bool:
        """Rebuild `records` without a rowid so each key is stored once."""
        sql = self.content.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()[0]
        if "WITHOUT ROWID" in sql.upper():
            return False
        with self.content:
            self.content.executescript(
                """
                CREATE TABLE records_migrated(
                    kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(kind,key)) WITHOUT ROWID;
                INSERT INTO records_migrated SELECT kind, key, value FROM records;
                DROP TABLE records;
                ALTER TABLE records_migrated RENAME TO records;
                """
            )
        return True

    def _migrate_chunk_text(self) -> bool:
        """Drop the inline copy of Chunk text that earlier versions also stored.

        Only a copy identical to the indexed text is dropped; a disagreeing copy
        stays inline and keeps precedence. The caller VACUUMs once afterwards.
        """
        if self._meta("chunk_text_store") == "chunks":
            return False
        stripped = 0
        with self.content:
            rows = self.content.execute(
                "SELECT key, value FROM records WHERE kind='node' "
                "AND json_extract(value, '$.label')='Chunk' "
                "AND json_type(value, '$.props.text') = 'text'"
            ).fetchall()
            for key, value in rows:
                record = strict_json(value)
                indexed = self.content.execute(
                    "SELECT text FROM chunks WHERE node_iri=?",
                    (self.node_iri("Chunk", record["id"]).value,),
                ).fetchone()
                if indexed is None or indexed[0] != record["props"]["text"]:
                    continue
                self.content.execute(
                    "UPDATE records SET value=? WHERE kind='node' AND key=?",
                    (canonical(self._stored_form("node", record)), key),
                )
                stripped += 1
            self._set_meta("chunk_text_store", "chunks")
        return bool(stripped)

    def _put(self, kind, record):
        key = self._key(kind, record)
        self.content.execute(
            "INSERT OR REPLACE INTO records VALUES (?,?,?)",
            (kind, key, canonical(self._stored_form(kind, record))),
        )
        if kind == "node":
            self._index_node(key, record)

    def _unput(self, kind, record):
        key = self._key(kind, record)
        self.content.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))
        if kind == "node":
            self.content.execute("DELETE FROM node_index WHERE key=?", (key,))
            if record["label"] == "Chunk":
                self.content.execute(
                    "DELETE FROM chunks WHERE node_iri=?",
                    (self.node_iri("Chunk", record["id"]).value,),
                )

    def _prepare_node_index(self) -> None:
        """Full-text index over non-Chunk node properties.

        Chunk text answers "what do the sources say"; lessons, conventions and
        findings live in node properties, and without this index a search could
        never surface them.
        """
        existed = self.content.execute(
            "SELECT 1 FROM sqlite_master WHERE name='node_index'"
        ).fetchone()
        self.content.executescript(
            """
            CREATE TABLE IF NOT EXISTS node_index (
                rowid INTEGER PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS node_index_label ON node_index(label);
            CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
                name, text, content='node_index', content_rowid='rowid',
                tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS node_index_ai AFTER INSERT ON node_index BEGIN
                INSERT INTO node_fts(rowid, name, text) VALUES (new.rowid, new.name, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS node_index_ad AFTER DELETE ON node_index BEGIN
                INSERT INTO node_fts(node_fts, rowid, name, text)
                    VALUES ('delete', old.rowid, old.name, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS node_index_au AFTER UPDATE ON node_index BEGIN
                INSERT INTO node_fts(node_fts, rowid, name, text)
                    VALUES ('delete', old.rowid, old.name, old.text);
                INSERT INTO node_fts(rowid, name, text) VALUES (new.rowid, new.name, new.text);
            END;
            """
        )
        if not existed:
            with self.content:
                for row in self.content.execute(
                    "SELECT key, value FROM records WHERE kind='node'"
                ).fetchall():
                    self._index_node(row[0], strict_json(row[1]))
        self.content.commit()

    def _index_node(self, key, record):
        self.content.execute("DELETE FROM node_index WHERE key=?", (key,))
        if record["label"] == "Chunk":
            return
        name = re.sub(r"[^\w]+", " ", f"{record['label']} {record['id']}").strip()
        text = "\n".join(_strings(record.get("props") or {}))
        self.content.execute(
            "INSERT INTO node_index(key, label, id, name, text) VALUES (?,?,?,?,?)",
            (key, record["label"], record["id"], name, text),
        )

    def _record(self, kind, key):
        row = self.content.execute(
            "SELECT value FROM records WHERE kind=? AND key=?", (kind, canonical(key))
        ).fetchone()
        return self._hydrate(strict_json(row[0])) if row else None

    def _records(self, kind):
        for row in self.content.execute(
            "SELECT value FROM records WHERE kind=? ORDER BY key", (kind,)
        ):
            yield self._hydrate(strict_json(row[0]))

    def _ensure_rdf(self):
        revision = self._meta("revision")
        if self._meta("rdf_revision") == revision:
            return
        # SQLite records are authoritative. An interrupted RDF rebuild is retried
        # on reopen before a query can observe it.
        self.store.clear()
        self.store.bulk_extend(self._quads())
        self._stamp_state(revision)
        self.store.flush()
        with self.content:
            self._set_meta("rdf_revision", revision)

    def _record_quads(self, kind, record):
        """The quads one neutral record contributes. Distinct records never share a
        quad, which is what makes removing a record's quads safe on its own."""
        if kind == "node":
            subject = self.node_iri(record["label"], record["id"])
            yield ox.Quad(subject, RDF_TYPE, ox.NamedNode(self.ns + record["label"]))
            yield ox.Quad(
                subject, ox.NamedNode(self.ns + "id"), ox.Literal(record["id"])
            )
            for prop, value in record.get("props", {}).items():
                if record["label"] == "Chunk" and prop == "text":
                    continue
                yield ox.Quad(subject, ox.NamedNode(self.ns + prop), literal(value))
            return
        subject = self.node_iri(record["src_label"], record["src"])
        predicate = ox.NamedNode(self.ns + record["rel"])
        obj = self.node_iri(record["dst_label"], record["dst"])
        yield ox.Quad(subject, predicate, obj)
        if record.get("props"):
            fact = self.edge_iri(record)
            for key, value in (
                ("_subject", subject),
                ("_predicate", predicate),
                ("_object", obj),
            ):
                yield ox.Quad(fact, ox.NamedNode(self.ns + key), value)
            for prop, value in record["props"].items():
                yield ox.Quad(
                    fact, ox.NamedNode(self.ns + "_edge_prop_" + prop), literal(value)
                )

    def _quads(self):
        for kind in ("node", "edge"):
            for record in self._records(kind):
                yield from self._record_quads(kind, record)

    def _apply_rdf_delta(self, base_revision, removed, changed):
        """Bring the RDF index forward by the records this write actually touched.

        A full rebuild after every write makes the cost of adding one fact
        proportional to the whole graph, which is what turns "note what I just
        learned" into an unusable operation on a large corpus. The rebuild stays
        as the recovery path: if the index was already behind, or this delta does
        not complete, `rdf_revision` is left stale and the next open rebuilds.
        """
        if self._meta("rdf_revision") != str(base_revision):
            return self._ensure_rdf()
        total = self.content.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        # Removing and adding quads one record at a time beats a rebuild only while
        # the change is small. Past that, the bulk load is cheaper, so a large
        # ingest still pays rebuild cost once instead of paying delta cost per record.
        if (len(removed) + len(changed)) * RDF_DELTA_RATIO > total:
            return self._ensure_rdf()
        for kind, record in removed:
            for quad in self._record_quads(kind, record):
                self.store.remove(quad)
        for kind, previous, incoming in changed:
            if previous is not None:
                for quad in self._record_quads(kind, previous):
                    self.store.remove(quad)
            self.store.extend(list(self._record_quads(kind, incoming)))
        self._stamp_state(self._meta("revision"))
        self.store.flush()
        with self.content:
            self._set_meta("rdf_revision", self._meta("revision"))

    # ----------------------------------------------------------------- storage
    @property
    def prefixes(self) -> str:
        return (
            f"PREFIX kg: <{self.ns}>\n"
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
            "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
        )

    def _prepare_content(self) -> None:
        self.content.executescript(
            """
            -- A rollback journal, not WAL: one process holds the graph at a time,
            -- so WAL buys no concurrency, and with WAL a committed write lived
            -- only in the -wal file until a checkpoint. A killed process plus a
            -- lost -wal file reopened the graph empty (reported twice).
            PRAGMA journal_mode=DELETE;
            -- Record rows run to ~1 KB; at 4 KiB pages a key-ordered table spills
            -- them to half-empty overflow pages. Applies to new files; existing
            -- ones adopt it at their next migration VACUUM.
            PRAGMA page_size=16384;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS chunks (
                rowid INTEGER PRIMARY KEY,
                node_iri TEXT NOT NULL UNIQUE,
                id TEXT NOT NULL,
                repository TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                document_id TEXT NOT NULL,
                heading TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                sha256 TEXT NOT NULL,
                loc TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
            CREATE INDEX IF NOT EXISTS chunks_repository ON chunks(repository);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                heading, text, content='chunks', content_rowid='rowid', tokenize='unicode61'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS locations_fts USING fts5(
                loc, tokenize='unicode61'
            );
            """
        )
        self._migrate_content()
        # Triggers keep both indexes in step with single-row writes. Without them
        # every merge had to rebuild the whole index, which turns "add one fact to
        # the graph" into work proportional to the entire corpus.
        self.content.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS chunks_after_insert AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, heading, text)
                    VALUES (new.rowid, new.heading, new.text);
                INSERT INTO locations_fts(rowid, loc) VALUES (new.rowid, new.loc);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_after_delete AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, heading, text)
                    VALUES ('delete', old.rowid, old.heading, old.text);
                DELETE FROM locations_fts WHERE rowid = old.rowid;
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_after_update AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, heading, text)
                    VALUES ('delete', old.rowid, old.heading, old.text);
                INSERT INTO chunks_fts(rowid, heading, text)
                    VALUES (new.rowid, new.heading, new.text);
                DELETE FROM locations_fts WHERE rowid = old.rowid;
                INSERT INTO locations_fts(rowid, loc) VALUES (new.rowid, new.loc);
            END;
            """
        )
        self.content.commit()

    def _migrate_content(self) -> None:
        """Bring a graph written by an earlier version up to the current schema."""
        columns = {
            row["name"] for row in self.content.execute("PRAGMA table_info(chunks)")
        }
        if "loc" in columns:
            return
        with self.content:
            self.content.execute(
                "ALTER TABLE chunks ADD COLUMN loc TEXT NOT NULL DEFAULT ''"
            )
            self.content.executemany(
                "UPDATE chunks SET loc = ? WHERE rowid = ?",
                [
                    (self._location_text(row["path"], row["heading"]), row["rowid"])
                    for row in self.content.execute(
                        "SELECT rowid, path, heading FROM chunks"
                    )
                ],
            )
            self.content.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            self.content.execute("DELETE FROM locations_fts")
            self.content.executemany(
                "INSERT INTO locations_fts(rowid, loc) VALUES (?,?)",
                [
                    (row["rowid"], row["loc"])
                    for row in self.content.execute("SELECT rowid, loc FROM chunks")
                ],
            )

    def node_iri(self, label: str, identifier: str) -> ox.NamedNode:
        return ox.NamedNode(f"{self.ns}{label}/{quote(str(identifier), safe='')}")

    def edge_iri(self, edge: dict) -> ox.NamedNode:
        identity = {
            key: edge[key] for key in ("rel", "src_label", "src", "dst_label", "dst")
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ox.NamedNode(f"{self.ns}_edge/{digest}")

    def iri(self, label: str, identifier: str) -> str:
        return f"<{self.node_iri(label, identifier).value}>"

    def decode(self, iri) -> str:
        value = iri.value if hasattr(iri, "value") else str(iri).strip("<>")
        return unquote(value.rsplit("/", 1)[-1])

    def exists(self, label: str, identifier: str) -> bool:
        return self._record("node", [label, identifier]) is not None

    # ------------------------------------------------------------------- write
    @staticmethod
    def _owners(record):
        props = record.get("props", {})
        return {
            props[k]
            for k in ("source", "repository")
            if isinstance(props.get(k), str) and props[k]
        }

    def clear(self):
        return self.load([], [], replace=True)

    def _drop_source(self, source):
        removed = set()
        removed_records = []
        for node in list(self._records("node")):
            if self._owners(node) == {source} or (
                node["label"] == "Source" and node["id"] == source
            ):
                removed.add((node["label"], node["id"]))
                removed_records.append(("node", node))
                self._unput("node", node)
        edges_removed = facts_removed = 0
        for edge in list(self._records("edge")):
            if (
                (edge["src_label"], edge["src"]) in removed
                or (edge["dst_label"], edge["dst"]) in removed
                or self._owners(edge) == {source}
            ):
                self._unput("edge", edge)
                removed_records.append(("edge", edge))
                edges_removed += 1
                facts_removed += bool(edge.get("props"))
        return {
            "source": source,
            "subjects_removed": len(removed),
            "edges_removed": edges_removed,
            "edge_facts_removed": facts_removed,
            "removed_records": removed_records,
        }

    @staticmethod
    def _report(result):
        return {key: value for key, value in result.items() if key != "removed_records"}

    def drop_source(self, source):
        if not isinstance(source, str) or not source:
            raise GraphValidationError("source must be a non-empty string")
        base = self._meta("revision")
        with self.content:
            result = self._drop_source(source)
            self._set_meta("revision", int(base) + 1)
        self._apply_rdf_delta(base, result["removed_records"], [])
        self._compact_after(len(result["removed_records"]))
        return self._report(result)

    def load(
        self,
        nodes,
        edges,
        replace: bool = False,
        source: str | None = None,
        on_conflict: str = "error",
        dry_run: bool = False,
        _prepare=None,
    ) -> dict:
        """Commit a complete additive batch; conflicting fields require an explicit policy.

        Source refresh and replacement are explicit destructive operations. The
        records and FTS changes commit together; RDF is a recoverable derived index.
        With dry_run, the whole batch runs inside the transaction and is rolled
        back: the report says what would be created or updated and lists every
        conflict instead of raising.
        """
        if on_conflict not in {"error", "incoming"}:
            raise GraphValidationError("on_conflict must be error or incoming")
        if replace and source is not None:
            raise GraphValidationError("replace and source are mutually exclusive")
        if source is not None and (not isinstance(source, str) or not source):
            raise GraphValidationError("source must be a non-empty string")
        nodes, edges = list(nodes), list(edges)
        # Validate all field types before destructive operations. Full endpoint
        # validation follows the source deletion inside the same rollbackable transaction.
        validate_graph(nodes, edges, resolve=self.exists if not replace else None)
        nodes = strict_json(canonical(nodes))
        for node in nodes:
            props = node.get("props") or {}
            if node["label"] == "Chunk" and "text" in props:
                digest = hashlib.sha256(props["text"].encode()).hexdigest()
                if "sha256" in props and props["sha256"] != digest:
                    raise GraphValidationError(
                        f"Chunk {node['id']}: text checksum mismatch"
                    )
                props["sha256"] = digest
                node["props"] = props
        if source is not None:
            for record in nodes + edges:
                owners = self._owners(record)
                if owners and owners != {source}:
                    raise GraphValidationError(
                        "source refresh batch contains another source"
                    )
                if record.get("label") == "Source" and record["id"] != source:
                    raise GraphValidationError(
                        "source refresh batch contains another Source"
                    )
        base_revision = self._meta("revision")
        try:
            report = self._load_transaction(
                nodes, edges, replace, source, on_conflict, dry_run, _prepare
            )
        except _DryRun as preview:
            return preview.args[0]
        (
            written,
            changed,
            created,
            updated,
            removed_records,
            changed_records,
            validation,
        ) = report
        if replace:
            # Everything is new, so a bulk rebuild beats a quad-by-quad delta.
            self._ensure_rdf()
        elif changed:
            self._apply_rdf_delta(base_revision, removed_records, changed_records)
        self._compact_after(changed)
        return {
            **validation,
            "mode": "replace"
            if replace
            else "replace_source"
            if source is not None
            else "merge",
            "created": created,
            "updated": updated,
            "triples": self._triple_count(),
            "chunks_written": written,
            "chunks_total": self.content.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0],
            "graph_store": str(self.graph_path),
            "content_store": str(self.content_path),
        }

    def _load_transaction(
        self, nodes, edges, replace, source, on_conflict, dry_run, prepare=None
    ):
        written = changed = created = updated = 0
        removed_records = []
        changed_records = []
        base_revision = self._meta("revision")
        with self.content:
            if replace:
                changed += self.content.execute(
                    "SELECT COUNT(*) FROM records"
                ).fetchone()[0]
                self.content.execute("DELETE FROM records")
                self.content.execute("DELETE FROM chunks")
                self.content.execute("DELETE FROM node_index")
            elif source is not None:
                dropped = self._drop_source(source)
                removed_records = dropped["removed_records"]
                changed += dropped["subjects_removed"] + dropped["edges_removed"]
            if prepare is not None:
                # Runs inside this transaction, so its removals and rewrites
                # commit (or roll back, or preview) together with the batch.
                extra_removed, extra_changed = prepare()
                removed_records = [*removed_records, *extra_removed]
                changed_records.extend(extra_changed)
                changed += len(extra_removed) + len(extra_changed)
            validation = validate_graph(
                nodes, edges, resolve=self.exists if not replace else None
            )
            conflicts = []
            for kind, records in (("node", nodes), ("edge", edges)):
                for incoming in records:
                    incoming = strict_json(canonical(incoming))
                    key = self._key(kind, incoming)
                    row = self.content.execute(
                        "SELECT value FROM records WHERE kind=? AND key=?", (kind, key)
                    ).fetchone()
                    previous = self._hydrate(strict_json(row[0])) if row else None
                    props = dict(previous.get("props", {})) if previous else {}
                    for prop, value in (incoming.get("props") or {}).items():
                        merged = _merge_set_prop(
                            kind, incoming, prop, props.get(prop), value
                        )
                        if merged is not None:
                            props[prop] = merged
                            continue
                        if (
                            prop in props
                            and canonical(props[prop]) != canonical(value)
                            and on_conflict == "error"
                        ):
                            # Collect every conflict before aborting: fixing a
                            # large merge one field per attempt is hopeless.
                            conflicts.append((kind, key, prop, props[prop], value))
                            continue
                        props[prop] = value
                    if (
                        kind == "node"
                        and incoming["label"] == "Chunk"
                        and "text" in (incoming.get("props") or {})
                        and "evicted" not in (incoming.get("props") or {})
                    ):
                        # Re-ingesting the text restores a chunk `compact` evicted.
                        props.pop("evicted", None)
                    if props or kind == "node" or "props" in incoming:
                        incoming["props"] = props
                    if previous and canonical(previous) == canonical(incoming):
                        continue
                    created += previous is None
                    updated += previous is not None
                    self._put(kind, incoming)
                    changed_records.append((kind, previous, incoming))
                    changed += 1
                    if kind == "node" and incoming["label"] == "Chunk":
                        self._write_chunk(incoming)
                        written += 1
            if dry_run:
                raise _DryRun(
                    {
                        **validation,
                        "dry_run": True,
                        "mode": "replace"
                        if replace
                        else "replace_source"
                        if source is not None
                        else "merge",
                        "created": created,
                        "updated": updated,
                        "removed": len(removed_records),
                        "conflicts": [
                            {
                                "kind": kind,
                                "key": key,
                                "field": prop,
                                "stored": current,
                                "incoming": value,
                            }
                            for kind, key, prop, current, value in conflicts
                        ],
                    }
                )
            if conflicts:
                raise GraphValidationError(conflict_message(conflicts))
            if changed:
                self._set_meta("revision", int(base_revision) + 1)
        return (
            written,
            changed,
            created,
            updated,
            removed_records,
            changed_records,
            validation,
        )

    def _write_chunk(self, node):
        props = node.get("props", {})
        subject = self.node_iri("Chunk", node["id"]).value
        self.content.execute("DELETE FROM chunks WHERE node_iri=?", (subject,))
        if "text" not in props:
            return
        text = props["text"]
        repository = props.get("repository") or props.get("source") or ""
        path = props.get("path") or node["id"]
        # Sources, documents and revisions are independent identity dimensions.
        document_id = canonical(
            [
                repository,
                props.get("source"),
                props.get("document_id") or path,
                props.get("revision"),
                props.get("document_sha256"),
            ]
        )
        self.content.execute(
            """INSERT INTO chunks
            (node_iri,id,repository,path,document_id,heading,text,start_line,end_line,sha256,loc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                subject,
                node["id"],
                repository,
                path,
                document_id,
                props.get("heading", ""),
                text,
                props.get("start_line"),
                props.get("end_line"),
                hashlib.sha256(text.encode()).hexdigest(),
                self._location_text(path, props.get("heading", "")),
            ),
        )

    @staticmethod
    def _location_text(path: str, heading: str) -> str:
        return re.sub(r"[^\w]+", " ", f"{path or ''} {heading or ''}").strip()

    # ---------------------------------------------------------------- transfer
    def _legacy_records(self):
        """Rebuild the neutral node and edge records from the two stores."""
        types: dict[str, str] = {}
        for subject, _, obj, _ in self.store.quads_for_pattern(None, RDF_TYPE, None):
            if obj.value.startswith(self.ns):
                types[subject.value] = obj.value.removeprefix(self.ns)

        nodes: dict[str, dict] = {}
        edges: dict[tuple, dict] = {}
        fact_props: dict[str, dict] = {}
        fact_parts: dict[str, dict] = {}
        edge_prefix = self.ns + "_edge_prop_"

        for subject, predicate, obj, _ in self.store.quads_for_pattern(
            None, None, None
        ):
            if predicate == RDF_TYPE or not predicate.value.startswith(self.ns):
                continue
            name = predicate.value.removeprefix(self.ns)
            if subject.value.startswith(self.ns + "_edge/"):
                if name in {"_subject", "_predicate", "_object"}:
                    fact_parts.setdefault(subject.value, {})[name] = obj.value
                elif predicate.value.startswith(edge_prefix):
                    fact_props.setdefault(subject.value, {})[
                        predicate.value.removeprefix(edge_prefix)
                    ] = _term_value(obj)
                continue
            label = types.get(subject.value)
            if label is None:
                continue
            if isinstance(obj, ox.NamedNode):
                other = types.get(obj.value)
                if other is None:
                    continue
                edges[(name, subject.value, obj.value)] = {
                    "rel": name,
                    "src_label": label,
                    "src": self.decode(subject),
                    "dst_label": other,
                    "dst": self.decode(obj),
                }
                continue
            record = nodes.setdefault(
                subject.value, {"label": label, "id": self.decode(subject), "props": {}}
            )
            if name != "id":
                record["props"][name] = _term_value(obj)

        for subject, label in types.items():
            nodes.setdefault(
                subject, {"label": label, "id": self.decode(subject), "props": {}}
            )
        for row in self.content.execute("SELECT node_iri, text FROM chunks"):
            if row["node_iri"] in nodes:
                nodes[row["node_iri"]]["props"]["text"] = row["text"]

        for fact, parts in fact_parts.items():
            props = fact_props.get(fact)
            if not props or len(parts) != 3:
                continue
            key = (
                parts["_predicate"].removeprefix(self.ns),
                parts["_subject"],
                parts["_object"],
            )
            if key in edges:
                edges[key]["props"] = props

        node_list = sorted(nodes.values(), key=lambda r: (r["label"], r["id"]))
        edge_list = sorted(
            edges.values(),
            key=lambda r: (
                r["rel"],
                r["src_label"],
                r["src"],
                r["dst_label"],
                r["dst"],
            ),
        )
        return node_list, edge_list

    def export_records(self):
        return list(self._records("node")), list(self._records("edge"))

    def export_package(
        self,
        path: str | Path,
        max_bytes: int = MAX_PACKAGE_BYTES,
        raw_roots=(),
        blobs: bool = True,
        large_bytes: int = LARGE_BLOB_BYTES,
        include_large: bool = False,
        usage: bool = True,
    ) -> dict:
        """Atomically export portable neutral records in a ZIP container.

        Referenced raw files are carried alongside the records as a
        content-addressed object store under blobs/, so an import can restore
        the evidence that Artifact records only point at.
        """
        path = Path(path)
        if not path.name.endswith(PACKAGE_SUFFIX):
            path = path.with_name(path.name + PACKAGE_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        nodes, edges = self.export_records()
        found = (
            resolve_blobs(
                nodes,
                raw_roots,
                large_bytes=large_bytes,
                include_large=include_large,
                stores=[self.blob_path],
            )
            if blobs
            else {"blobs": {}, "missing": [], "changed": [], "oversized": []}
        )
        # Sizes only: bytes are streamed into the archive one blob at a time.
        stored = {
            digest: source.stat().st_size
            for digest, source in sorted(found["blobs"].items())
        }
        self._flush_usage()  # this process's own use belongs in the package too
        usage_rows = self.usage_rows() if usage else []
        members = [("nodes.jsonl", nodes), ("edges.jsonl", edges)]
        if usage_rows:
            members.append((USAGE_MEMBER, usage_rows))
        payloads = {
            name: "".join(canonical(r) + "\n" for r in records).encode("utf-8")
            for name, records in members
        }
        manifest = {
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION if usage_rows else 3,
            "graph": self.name,
            "namespace": self.ns,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": {"nodes": len(nodes), "edges": len(edges)},
            "labels": dict(sorted(Counter(n["label"] for n in nodes).items())),
            "rels": dict(sorted(Counter(e["rel"] for e in edges).items())),
            "sources": sorted(
                {n["id"] for n in nodes if n["label"] == "Source"}
                | set().union(*(self._owners(n) for n in nodes))
            ),
            "members": {
                name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in payloads.items()
            },
            "blobs": dict(stored),
        }
        if usage_rows:
            manifest["usage"] = {**self._usage_meta(), "rows": len(usage_rows)}
        payloads["manifest.json"] = canonical(manifest).encode("utf-8")
        if sum(map(len, payloads.values())) + sum(stored.values()) > max_bytes:
            raise GraphValidationError(
                "package exceeds max_bytes; shard the corpus, drop blobs with "
                "--no-blobs, or explicitly raise the limit"
            )
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w+b") as stream:
                with zipfile.ZipFile(
                    stream, "w", zipfile.ZIP_DEFLATED, compresslevel=6
                ) as archive:
                    for name, data in payloads.items():
                        archive.writestr(name, data)
                    for digest, source in sorted(found["blobs"].items()):
                        with (
                            source.open("rb") as raw,
                            archive.open(
                                BLOB_PREFIX + digest, "w", force_zip64=True
                            ) as member,
                        ):
                            # Re-hash while copying: a file edited after it was
                            # resolved must not be packed under the old digest.
                            _copy_verified(raw, member, digest)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {
            **manifest,
            "package": str(path),
            "bytes": path.stat().st_size,
            "blobs": {
                "stored": len(stored),
                "bytes": sum(stored.values()),
                "missing": found["missing"],
                "changed": found["changed"],
                "oversized": found["oversized"],
            },
        }

    @staticmethod
    def read_package(
        path: str | Path, max_bytes: int = MAX_PACKAGE_BYTES, with_usage: bool = False
    ):
        """Validate a package; returns (manifest, nodes, edges[, usage rows])."""
        path = Path(path)
        if max_bytes <= 0:
            raise GraphValidationError("max_bytes must be positive")
        if not zipfile.is_zipfile(path):
            raise GraphValidationError(f"{path}: not a zip archive")
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                missing = PACKAGE_MEMBERS - set(names)
                if missing:
                    raise GraphValidationError(
                        f"{path}: archive is missing {sorted(missing)}"
                    )
                blob_names = [name for name in names if name.startswith(BLOB_PREFIX)]
                if len(names) != len(set(names)) or set(names) - PACKAGE_MEMBERS - {
                    USAGE_MEMBER
                } - set(blob_names):
                    raise GraphValidationError(
                        "duplicate or unexpected archive members"
                    )
                if any(
                    not BLOB_NAME.match(name[len(BLOB_PREFIX) :]) for name in blob_names
                ):
                    raise GraphValidationError(
                        "blob members must be named by their sha256 digest"
                    )
                if sum(entry.file_size for entry in entries) > max_bytes:
                    raise GraphValidationError("package exceeds max_bytes")
                if archive.getinfo("manifest.json").file_size > 1024 * 1024:
                    raise GraphValidationError("manifest exceeds 1 MiB")
                for entry in entries:
                    mode = (entry.external_attr >> 16) & 0o170000
                    if mode not in (0, 0o100000) or entry.flag_bits & 1:
                        raise GraphValidationError(
                            "encrypted or non-regular archive member"
                        )
                    if entry.compress_type not in (
                        zipfile.ZIP_STORED,
                        zipfile.ZIP_DEFLATED,
                    ):
                        raise GraphValidationError("unsupported ZIP compression")
                manifest = strict_json(archive.read("manifest.json"))
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("format") != PACKAGE_FORMAT
                ):
                    raise GraphValidationError(
                        f"{path}: not a {PACKAGE_FORMAT} archive"
                    )
                version = manifest.get("version")
                if type(version) is not int or not 1 <= version <= PACKAGE_VERSION:
                    raise GraphValidationError("unsupported package version")
                declared = manifest.get("blobs") if version >= 3 else {}
                if not isinstance(declared, dict) or any(
                    not isinstance(digest, str)
                    or not BLOB_NAME.match(digest)
                    or type(size) is not int
                    or size < 0
                    for digest, size in declared.items()
                ):
                    raise GraphValidationError("invalid manifest blobs")
                if set(declared) != {name[len(BLOB_PREFIX) :] for name in blob_names}:
                    raise GraphValidationError("manifest blob set mismatch")
                for name in blob_names:
                    entry = archive.getinfo(name)
                    if entry.file_size != declared[name[len(BLOB_PREFIX) :]]:
                        raise GraphValidationError(f"{name}: byte count mismatch")
                counts = manifest.get("counts")
                if not isinstance(counts, dict) or any(
                    type(counts.get(k)) is not int or counts[k] < 0
                    for k in ("nodes", "edges")
                ):
                    raise GraphValidationError("invalid manifest counts")
                has_usage = USAGE_MEMBER in names
                if has_usage != (version >= 4 and "usage" in manifest):
                    raise GraphValidationError(
                        f"{USAGE_MEMBER} and the manifest usage entry must come together "
                        "in a version 4 package"
                    )
                payloads = {
                    name: archive.read(name)
                    for name in ("nodes.jsonl", "edges.jsonl")
                    + ((USAGE_MEMBER,) if has_usage else ())
                }
                if version >= 2:
                    checks = manifest.get("members")
                    if not isinstance(checks, dict) or set(checks) != set(payloads):
                        raise GraphValidationError("missing member checksums")
                    for name, data in payloads.items():
                        check = checks[name]
                        if (
                            not isinstance(check, dict)
                            or type(check.get("bytes")) is not int
                            or check["bytes"] != len(data)
                        ):
                            raise GraphValidationError(f"{name}: byte count mismatch")
                        if check.get("sha256") != hashlib.sha256(data).hexdigest():
                            raise GraphValidationError(f"{name}: checksum mismatch")
                usage_rows = (
                    _usage_rows(payloads[USAGE_MEMBER], manifest["usage"])
                    if has_usage
                    else []
                )
                records = []
                for name in ("nodes.jsonl", "edges.jsonl"):
                    records.append(
                        [
                            strict_json(line)
                            for line in payloads[name].decode("utf-8").splitlines()
                            if line.strip()
                        ]
                    )
                nodes, edges = records
                if len(nodes) != counts["nodes"] or len(edges) != counts["edges"]:
                    raise GraphValidationError("manifest record count mismatch")
                validate_graph(nodes, edges)
                if version >= 2:
                    actual_sources = sorted(
                        {n["id"] for n in nodes if n["label"] == "Source"}
                        | set().union(*(KG._owners(n) for n in nodes))
                    )
                    if manifest.get("sources") != actual_sources:
                        raise GraphValidationError("manifest sources mismatch")
                for field, actual in (
                    ("labels", Counter(n["label"] for n in nodes)),
                    ("rels", Counter(e["rel"] for e in edges)),
                ):
                    if field in manifest and manifest[field] != dict(actual):
                        raise GraphValidationError(f"manifest {field} mismatch")
                if with_usage:
                    return manifest, nodes, edges, usage_rows
                return manifest, nodes, edges
        except (
            ValueError,
            UnicodeError,
            zipfile.BadZipFile,
            RuntimeError,
            NotImplementedError,
        ) as exc:
            if isinstance(exc, GraphValidationError):
                raise
            raise GraphValidationError(f"{path}: invalid graph package: {exc}") from exc

    def import_package(
        self,
        path: str | Path,
        replace: bool = False,
        on_conflict: str = "error",
        max_bytes: int = MAX_PACKAGE_BYTES,
        blob_dir: str | Path | None = None,
        dry_run: bool = False,
        usage: bool = True,
        memory_only: bool = False,
    ) -> dict:
        """Merge a package. With `memory_only`, take only the memory notes and the
        nodes their edges reach: the way to merge a fork's lessons without bringing
        back source versions this graph has already refreshed or pruned."""
        manifest, nodes, edges, usage_rows = self.read_package(
            path, max_bytes=max_bytes, with_usage=True
        )
        selection = None
        if memory_only:
            memory = {
                (n["label"], n["id"]) for n in nodes if n["label"] in MEMORY_LABELS
            }
            edges = [
                e
                for e in edges
                if (e["src_label"], e["src"]) in memory
                or (e["dst_label"], e["dst"]) in memory
            ]
            reached = {
                end
                for e in edges
                for end in ((e["src_label"], e["src"]), (e["dst_label"], e["dst"]))
            } - memory
            nodes = [n for n in nodes if (n["label"], n["id"]) in memory | reached]
            selection = {
                "memory_nodes": len(memory),
                "evidence_nodes": len(reached),
                "edges": len(edges),
            }
        if dry_run:
            return {
                **self.load(
                    nodes, edges, replace=replace, on_conflict=on_conflict, dry_run=True
                ),
                "package": str(path),
                "manifest": manifest,
                **({"memory_only": selection} if selection else {}),
            }
        # Blobs are verified and kept before any record changes: a package whose
        # bytes do not match their digests is rejected as a whole, and the bytes
        # that do arrive stay with the graph for its next export.
        kept = extract_blobs(path, self.blob_path)
        result = self.load(nodes, edges, replace=replace, on_conflict=on_conflict)
        merged = 0
        if usage and (usage_rows or replace):
            merged = self._merge_usage(
                usage_rows,
                manifest.get("usage") or {"generation": 0, "since": usage_day()},
                replace=replace,
            )
        blobs = extract_blobs(path, blob_dir) if blob_dir else skipped_blobs(manifest)
        blobs["kept"] = kept["extracted"]
        blobs["store"] = kept["dir"]
        return {
            **result,
            "package": str(path),
            "manifest": manifest,
            "integrity": "sha256"
            if manifest["version"] >= 2
            else "legacy-no-checksums",
            "blobs": blobs,
            "usage": {"rows": len(usage_rows), "merged": merged},
            **({"memory_only": selection} if selection else {}),
        }

    # -------------------------------------------------------------------- read
    def q(self, sparql: str):
        self._ensure_rdf()
        result = self.store.query(self.prefixes + sparql)
        if isinstance(result, ox.QueryBoolean):
            return bool(result)
        rows = []
        for row in result:
            values = [_term_value(value) for value in row]
            rows.append(values[0] if len(values) == 1 else values)
        return rows

    def props(self, label: str, identifier: str) -> dict:
        record = self._record("node", [label, identifier])
        return {"id": identifier, **record.get("props", {})} if record else {}

    def edge_props(self, rel, src_label, src, dst_label, dst) -> dict:
        record = self._record("edge", [rel, src_label, src, dst_label, dst])
        return record.get("props", {}) if record else {}

    def neighbors(
        self, label: str, identifier: str, rels=(), inbound: bool = False
    ) -> list[dict]:
        self._ensure_rdf()
        target = self.node_iri(label, identifier)
        pattern = (None, None, target) if inbound else (target, None, None)
        result = []
        internal = {"_subject", "_predicate", "_object"}
        for subject, predicate, obj, _ in self.store.quads_for_pattern(*pattern):
            rel = predicate.value.removeprefix(self.ns)
            other = subject if inbound else obj
            if (
                predicate == RDF_TYPE
                or not isinstance(other, ox.NamedNode)
                or rel in internal
            ):
                continue
            if rels and rel not in rels:
                continue
            other_type = next(
                (
                    value.value.removeprefix(self.ns)
                    for _, _, value, _ in self.store.quads_for_pattern(
                        other, RDF_TYPE, None
                    )
                ),
                None,
            )
            result.append({"rel": rel, "label": other_type, "id": self.decode(other)})
        return sorted(
            result, key=lambda row: (row["rel"], row["label"] or "", row["id"])
        )

    def reach(self, label, identifier, rels, node_label=None) -> list[str]:
        if not rels:
            raise ValueError("reach requires at least one relation")
        for rel in rels:
            _check_name("relation", rel)
        relation_path = "|".join(f"kg:{rel}" for rel in rels)
        label_pattern = (
            f"?target a kg:{_check_name('label', node_label)} ." if node_label else ""
        )
        return self.q(
            f"""SELECT DISTINCT ?id WHERE {{
            {self.iri(label, identifier)} ({relation_path})+ ?target .
            {label_pattern}
            ?target kg:id ?id .
            }} ORDER BY ?id"""
        )

    def path(self, start_label, start_id, end_label, end_id, rels, max_hops: int = 12):
        if not rels:
            raise ValueError("path requires at least one relation")
        if max_hops < 0:
            raise ValueError("max_hops must be nonnegative")
        for rel in rels:
            _check_name("relation", rel)
        start, end = (start_label, start_id), (end_label, end_id)
        if not self.exists(*start) or not self.exists(*end):
            return []
        queue = deque([(start, [start_id])])
        seen = {start}
        while queue:
            current, chain = queue.popleft()
            if current == end:
                return chain
            if len(chain) - 1 >= max_hops:
                continue
            for item in self.neighbors(*current, rels=rels):
                target = (item["label"], item["id"])
                if target not in seen:
                    seen.add(target)
                    queue.append((target, chain + [item["id"]]))
        return []

    # ------------------------------------------------------------------ search
    @staticmethod
    def _fts_phrase(term: str) -> str:
        return '"' + term.replace('"', '""') + '"'

    def document_frequency(self, term: str, table: str = "text", source=None) -> int:
        index = "chunks_fts" if table == "text" else "locations_fts"
        phrase = self._fts_phrase(term) + ("*" if table == "location" else "")
        predicate, params = (
            (" AND c.repository = ?", [phrase, source])
            if source is not None
            else ("", [phrase])
        )
        return self.content.execute(
            f"SELECT COUNT(*) FROM {index} JOIN chunks c ON c.rowid={index}.rowid WHERE {index} MATCH ?{predicate}",
            params,
        ).fetchone()[0]

    def _hits(self, term: str, table: str, source=None, candidate_limit=20000):
        index = "chunks_fts" if table == "text" else "locations_fts"
        phrase = self._fts_phrase(term) + ("*" if table == "location" else "")
        predicate, params = (
            (" AND c.repository = ?", [phrase, source])
            if source is not None
            else ("", [phrase])
        )
        return self.content.execute(
            f"SELECT c.*, bm25({index}) AS rank FROM {index} JOIN chunks c ON c.rowid={index}.rowid "
            f"WHERE {index} MATCH ?{predicate} ORDER BY rank, c.node_iri LIMIT ?",
            [*params, candidate_limit],
        ).fetchall()

    def _evidence(self, row, terms):
        text = row["text"]
        offsets = [text.casefold().find(term.casefold()) for term in terms]
        first = min((position for position in offsets if position >= 0), default=0)
        start = max(0, first - 240) if len(text) > 1200 else 0
        start = min(start, max(0, len(text) - 1200))
        props = self.props("Chunk", row["id"])
        return {
            **props,
            "id": row["id"],
            "heading": row["heading"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "text": text[start : start + 1200],
            "truncated": len(text) > 1200,
            "snippet_start_char": start,
            "snippet_end_char": min(len(text), start + 1200),
            "line_range_basis": "whole_chunk",
        }

    def search(
        self,
        terms,
        limit: int = 10,
        scope: str = "file",
        require_all: bool = False,
        min_score: float = MIN_SCORE,
        source: str | None = None,
        candidate_limit: int = 20000,
        nodes: int = 5,
    ) -> dict:
        """Rank lexical evidence; an unmatched term is not proof of factual absence.

        Up to `nodes` matching knowledge nodes (lessons, findings, domain nodes)
        are returned beside the chunk results; 0 turns that off.
        """
        if (
            scope not in {"file", "chunk"}
            or limit < 0
            or candidate_limit <= 0
            or not math.isfinite(min_score)
        ):
            raise GraphValidationError(
                "invalid search scope, limit, candidate limit or score"
            )
        terms = [t for t in dict.fromkeys((t or "").strip() for t in terms or []) if t]

        def answer(unmatched, results, capped, node_hits):
            # One shape for every outcome, so callers never probe for keys.
            return {
                "terms": terms,
                "unmatched": unmatched,
                "results": results,
                "candidate_limit": candidate_limit,
                "capped_terms": capped,
                "nodes": node_hits,
            }

        if not terms:
            return answer([], [], [], [])
        node_hits = (
            self.find(terms, limit=nodes, require_all=require_all, source=source)
            if nodes > 0
            else []
        )

        text_df = {
            term: self.document_frequency(term, "text", source) for term in terms
        }
        location_df = {
            term: self.document_frequency(term, "location", source) for term in terms
        }
        # A term that only ever appears in a path still identifies a file, so it
        # counts as matched; only a term found nowhere at all is absent.
        matched = [term for term in terms if text_df[term] or location_df[term]]
        unmatched = [term for term in terms if not (text_df[term] or location_df[term])]
        if not matched or (require_all and unmatched):
            return answer(unmatched, [], [], node_hits)

        total_chunks = (
            self.content.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 1
        )
        idf = {
            term: math.log(1 + total_chunks / (text_df[term] or location_df[term]))
            for term in matched
        }
        weight = sum(idf.values()) or 1.0
        text_hits = {
            term: self._hits(term, "text", source, candidate_limit)
            if text_df[term]
            else []
            for term in matched
        }
        location_hits = {
            term: self._hits(term, "location", source, candidate_limit)
            if location_df[term]
            else []
            for term in matched
        }
        capped = [
            term
            for term in matched
            if text_df[term] > len(text_hits[term])
            or location_df[term] > len(location_hits[term])
        ]
        buckets: dict[str, dict] = {}

        def bucket_for(row):
            key = row["node_iri"] if scope == "chunk" else row["document_id"]
            return buckets.setdefault(
                key,
                {
                    "repository": row["repository"],
                    "path": row["path"],
                    "terms": set(),
                    "location_terms": set(),
                    "best": 0.0,
                    "hits": {},
                },
            )

        for term in matched:
            for row in text_hits[term]:
                if source and row["repository"] != source:
                    continue
                bucket = bucket_for(row)
                bucket["terms"].add(term)
                bucket["best"] = min(bucket["best"], float(row["rank"]))
                bucket["hits"][row["node_iri"]] = row
            for row in location_hits[term]:
                if source and row["repository"] != source:
                    continue
                bucket = bucket_for(row)
                bucket["location_terms"].add(term)
                bucket["hits"][row["node_iri"]] = row

        scored = []
        for bucket in buckets.values():
            if require_all and len(bucket["terms"] | bucket["location_terms"]) != len(
                matched
            ):
                continue
            covered = sum(idf[t] for t in bucket["terms"]) / weight
            located = sum(idf[t] for t in bucket["location_terms"]) / weight
            score = covered + PATH_WEIGHT * located
            if not require_all and score < min_score:
                continue
            scored.append((score, covered, bucket))
        scored.sort(
            key=lambda item: (
                -round(item[0], 4),
                item[2]["best"],
                item[2]["repository"],
                item[2]["path"],
            )
        )

        results = []
        for score, covered, bucket in scored[:limit]:
            hits = sorted(
                bucket["hits"].values(),
                key=lambda hit: (
                    -sum(term.casefold() in hit["text"].casefold() for term in matched),
                    hit["start_line"] or 0,
                    hit["id"],
                ),
            )
            evidence = [self._evidence(row, matched) for row in hits[:8]]
            results.append(
                {
                    "repository": bucket["repository"],
                    "path": bucket["path"],
                    "score": round(score, 3),
                    "coverage": round(covered, 3),
                    "matched_terms": sorted(bucket["terms"] | bucket["location_terms"]),
                    "evidence": evidence,
                    "evidence_truncated": len(hits) > 8,
                    # The Document node ID; the grouping key stays internal.
                    "document_id": evidence[0].get("document_id") if evidence else None,
                }
            )
        return answer(unmatched, results, capped, node_hits)

    def find(
        self,
        query,
        labels=None,
        limit: int = 10,
        require_all: bool = False,
        source: str | None = None,
        width: int = 300,
    ) -> list[dict]:
        """Rank non-Chunk nodes by their label, ID and string properties.

        `query` is a question (tokenized) or a list of exact phrases. Labels the
        ingester makes are skipped unless named in `labels`, so lessons and
        findings are not drowned by thousands of URL and variable mentions.
        """
        terms = tokenize(query) if isinstance(query, str) else list(query or [])
        terms = [t for t in dict.fromkeys(t.strip() for t in terms if t) if t]
        if not terms or limit <= 0:
            return []
        match = (" AND " if require_all else " OR ").join(
            self._fts_phrase(term) for term in terms
        )
        predicates, params = ["node_fts MATCH ?"], [match]
        if labels:
            for label in labels:
                _check_name("label", label)
            predicates.append(f"n.label IN ({','.join('?' * len(labels))})")
            params.extend(labels)
        else:
            predicates.append(f"n.label NOT IN ({','.join('?' * len(INGEST_LABELS))})")
            params.extend(sorted(INGEST_LABELS))
        rows = self.content.execute(
            "SELECT n.key, n.label, n.id, n.name, n.text, "
            "bm25(node_fts, 2.0, 1.0) AS rank FROM node_fts "
            "JOIN node_index n ON n.rowid = node_fts.rowid "
            f"WHERE {' AND '.join(predicates)} ORDER BY rank, n.key LIMIT ?",
            [*params, max(limit * 5, 50)],
        ).fetchall()
        hits = []
        for row in rows:
            record = self._record("node", [row["label"], row["id"]]) or {}
            props = record.get("props") or {}
            if source is not None and source not in self._owners(record):
                continue
            haystack = f"{row['name']}\n{row['text']}".casefold()
            matched = [t for t in terms if t.casefold() in haystack]
            hits.append(
                {
                    "label": row["label"],
                    "id": row["id"],
                    "score": round(len(matched) / len(terms), 3),
                    "matched_terms": matched,
                    "props": {
                        key: (
                            value
                            if not isinstance(value, str) or len(value) <= width
                            else value[: width - 3] + "..."
                        )
                        for key, value in props.items()
                    },
                    "_rank": row["rank"],
                }
            )
        # Coverage first, then BM25: a node naming every term beats one that
        # repeats a single term many times.
        hits.sort(
            key=lambda hit: (-hit["score"], hit["_rank"], hit["label"], hit["id"])
        )
        for hit in hits:
            del hit["_rank"]
        return hits[:limit]

    def brief(self, labels=None, per_label: int = 10, width: int = 200) -> dict:
        """Session-start digest: memory nodes newest first, other labels counted."""
        counts = Counter(
            {
                row[0]: row[1]
                for row in self.content.execute(
                    "SELECT json_extract(value, '$.label'), COUNT(*) FROM records "
                    "WHERE kind='node' GROUP BY 1"
                )
            }
        )
        wanted = list(labels) if labels else [l for l in MEMORY_LABELS if counts[l]]
        memory = {}
        for label in wanted:
            _check_name("label", label)
            items = []
            for row in self.content.execute(
                "SELECT value FROM records WHERE kind='node' "
                "AND json_extract(value, '$.label') = ?",
                (label,),
            ):
                record = self._hydrate(strict_json(row[0]))
                props = record.get("props") or {}
                items.append(
                    {
                        "id": record["id"],
                        "date": node_date(props),
                        "summary": node_summary(props, width),
                        "source": props.get("source") or props.get("repository") or "",
                    }
                )
            items.sort(key=lambda item: (item["date"], item["id"]), reverse=True)
            memory[label] = {"count": len(items), "items": items[:per_label]}
        return {
            "graph": self.name,
            "nodes": sum(counts.values()),
            "sources": len(self.sources()),
            "memory": memory,
            "other_labels": {
                label: count
                for label, count in sorted(counts.items())
                if label not in memory
            },
        }

    def suggest(self, label: str, identifier: str, limit: int = 5) -> list[str]:
        """Nearby node references for a lookup that found nothing."""
        import difflib

        rows = self.content.execute(
            "SELECT label, id FROM node_index WHERE label = ? OR id = ?",
            (label, identifier),
        ).fetchall()
        exact = [f"{row[0]}:{row[1]}" for row in rows if row[1] == identifier]
        same = [row[1] for row in rows if row[0] == label]
        close = [i for i in same if identifier.casefold() in i.casefold()]
        close += difflib.get_close_matches(identifier, same, n=limit, cutoff=0.6)
        return list(dict.fromkeys(exact + [f"{label}:{i}" for i in sorted(close)]))[
            :limit
        ]

    # ------------------------------------------------------------------ remove
    def orphans(self, labels) -> list[tuple[str, str]]:
        """Nodes of the given labels with no incident edge at all."""
        for label in labels:
            _check_name("label", label)
        linked = set()
        for row in self.content.execute("SELECT value FROM records WHERE kind='edge'"):
            edge = strict_json(row[0])
            linked.add((edge["src_label"], edge["src"]))
            linked.add((edge["dst_label"], edge["dst"]))
        found = []
        for label in labels:
            for row in self.content.execute(
                "SELECT json_extract(value, '$.id') FROM records WHERE kind='node' "
                "AND json_extract(value, '$.label') = ? ORDER BY key",
                (label,),
            ):
                if (label, row[0]) not in linked:
                    found.append((label, row[0]))
        return found

    def delete(self, nodes=(), edges=(), dry_run: bool = False) -> dict:
        """Explicitly remove nodes (with every incident edge) and edges.

        `nodes` holds (label, id) pairs, `edges` neutral edge identities. Unknown
        references are reported, not fatal. Destructive: preview with dry_run.
        """
        targets = {(label, identifier) for label, identifier in nodes}
        edge_keys = {self._key("edge", edge) for edge in edges}
        missing = sorted(
            f"{label}:{identifier}"
            for label, identifier in targets
            if not self.exists(label, identifier)
        )
        removed = []
        for label, identifier in sorted(targets):
            record = self._record("node", [label, identifier])
            if record:
                removed.append(("node", record))
        for row in self.content.execute(
            "SELECT key, value FROM records WHERE kind='edge'"
        ).fetchall():
            edge = strict_json(row[1])
            if (
                row[0] in edge_keys
                or (edge["src_label"], edge["src"]) in targets
                or (edge["dst_label"], edge["dst"]) in targets
            ):
                removed.append(("edge", edge))
        report = {
            "dry_run": dry_run,
            "nodes_removed": sum(kind == "node" for kind, _ in removed),
            "edges_removed": sum(kind == "edge" for kind, _ in removed),
            "missing": missing,
        }
        if dry_run or not removed:
            return report
        base = self._meta("revision")
        with self.content:
            for kind, record in removed:
                self._unput(kind, record)
            self._set_meta("revision", int(base) + 1)
        self._apply_rdf_delta(base, removed, [])
        self._compact_after(len(removed))
        return report

    def search_keywords(
        self, keywords, limit=10, scope="file", require_all=True, source=None
    ):
        return self.search(
            keywords, limit=limit, scope=scope, require_all=require_all, source=source
        )["results"]

    def search_question(
        self, question: str, limit: int = 10, source: str | None = None
    ):
        return self.search(
            tokenize(question), limit=limit, scope="file", source=source
        )["results"]

    # ------------------------------------------------------------------- stats
    def sources(self) -> list[str]:
        """Source names exactly as a package manifest lists them: Source node
        IDs plus every node's string `source`/`repository` owner tag."""
        rows = self.content.execute(
            """SELECT json_extract(value, '$.id') FROM records
               WHERE kind='node' AND json_extract(value, '$.label') = 'Source'
               UNION
               SELECT json_extract(value, '$.props.source') FROM records
               WHERE kind='node' AND json_type(value, '$.props.source') = 'text'
               UNION
               SELECT json_extract(value, '$.props.repository') FROM records
               WHERE kind='node' AND json_type(value, '$.props.repository') = 'text'"""
        )
        return sorted({row[0] for row in rows if row[0]})

    def stats(self) -> dict:
        self._ensure_rdf()
        labels = Counter()
        for _, _, obj, _ in self.store.quads_for_pattern(None, RDF_TYPE, None):
            if obj.value.startswith(self.ns):
                labels[obj.value.removeprefix(self.ns)] += 1
        rels = Counter()
        for _, predicate, obj, _ in self.store.quads_for_pattern(None, None, None):
            rel = predicate.value.removeprefix(self.ns)
            if (
                isinstance(obj, ox.NamedNode)
                and predicate != RDF_TYPE
                and predicate.value.startswith(self.ns)
                and not rel.startswith("_")
            ):
                rels[rel] += 1
        chunks = self.content.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sources = self.sources()
        size = sum(p.stat().st_size for p in self.graph_path.rglob("*") if p.is_file())
        size += sum(
            p.stat().st_size
            for p in self.content_path.parent.glob(self.content_path.name + "*")
            if p.is_file()
        )
        return {
            "triples": self._triple_count(),
            "nodes": sum(labels.values()),
            "chunks": chunks,
            "sources": sources,
            "labels": dict(sorted(labels.items())),
            "rels": dict(sorted(rels.items())),
            "graph_store": str(self.graph_path),
            "content_store": str(self.content_path),
            "bytes": size,
        }
