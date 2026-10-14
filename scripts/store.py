#!/usr/bin/env python3
"""Persistent RDF topology plus SQLite FTS content for knowledge graphs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import zipfile
from collections import Counter, deque
from datetime import datetime, timezone
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
PACKAGE_VERSION = 2
PACKAGE_MEMBERS = {"manifest.json", "nodes.jsonl", "edges.jsonl"}
MAX_PACKAGE_BYTES = 512 * 1024 * 1024


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


class GraphValidationError(ValueError):
    """Neutral graph violates the interchange contract."""


def graph_home() -> Path:
    return Path(os.environ.get("KG_HOME", ".kg"))


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
        self.file = self.graph_path
        rdf_existed = self.graph_path.exists()
        if not create and not (self.graph_path.exists() or self.content_path.exists()):
            raise FileNotFoundError(f"no graph {name!r} at {self.home}")
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.content_path.parent.mkdir(parents=True, exist_ok=True)
        # Acquire the exclusive graph lock before touching SQLite.
        self.store = ox.Store(str(self.graph_path))
        try:
            self.content = sqlite3.connect(self.content_path)
            self.content.row_factory = sqlite3.Row
            self._prepare_content()
            self.content.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(kind,key));
            """)
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
            if not saved and len(self.store) and not inferred:
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

    def _put(self, kind, record):
        self.content.execute(
            "INSERT OR REPLACE INTO records VALUES (?,?,?)",
            (kind, self._key(kind, record), canonical(record)),
        )

    def _record(self, kind, key):
        row = self.content.execute(
            "SELECT value FROM records WHERE kind=? AND key=?", (kind, canonical(key))
        ).fetchone()
        return strict_json(row[0]) if row else None

    def _records(self, kind):
        for row in self.content.execute(
            "SELECT value FROM records WHERE kind=? ORDER BY key", (kind,)
        ):
            yield strict_json(row[0])

    def _ensure_rdf(self):
        revision = self._meta("revision")
        if self._meta("rdf_revision") == revision:
            return
        # SQLite records are authoritative. An interrupted RDF rebuild is retried
        # on reopen before a query can observe it.
        self.store.clear()
        self.store.bulk_extend(self._quads())
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
            PRAGMA journal_mode=WAL;
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
                self.content.execute(
                    "DELETE FROM records WHERE kind='node' AND key=?",
                    (self._key("node", node),),
                )
                self.content.execute(
                    "DELETE FROM chunks WHERE node_iri=?",
                    (self.node_iri(node["label"], node["id"]).value,),
                )
        edges_removed = facts_removed = 0
        for edge in list(self._records("edge")):
            if (
                (edge["src_label"], edge["src"]) in removed
                or (edge["dst_label"], edge["dst"]) in removed
                or self._owners(edge) == {source}
            ):
                self.content.execute(
                    "DELETE FROM records WHERE kind='edge' AND key=?",
                    (self._key("edge", edge),),
                )
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
        return self._report(result)

    def load(
        self,
        nodes,
        edges,
        replace: bool = False,
        source: str | None = None,
        on_conflict: str = "error",
    ) -> dict:
        """Commit a complete additive batch; conflicting fields require an explicit policy.

        Source refresh and replacement are explicit destructive operations. The
        records and FTS changes commit together; RDF is a recoverable derived index.
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
        written = 0
        changed = 0
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
            elif source is not None:
                dropped = self._drop_source(source)
                removed_records = dropped["removed_records"]
                changed += dropped["subjects_removed"] + dropped["edges_removed"]
            validation = validate_graph(
                nodes, edges, resolve=self.exists if not replace else None
            )
            for kind, records in (("node", nodes), ("edge", edges)):
                for incoming in records:
                    incoming = strict_json(canonical(incoming))
                    key = self._key(kind, incoming)
                    row = self.content.execute(
                        "SELECT value FROM records WHERE kind=? AND key=?", (kind, key)
                    ).fetchone()
                    previous = strict_json(row[0]) if row else None
                    props = dict(previous.get("props", {})) if previous else {}
                    for prop, value in (incoming.get("props") or {}).items():
                        if (
                            prop in props
                            and canonical(props[prop]) != canonical(value)
                            and on_conflict == "error"
                        ):
                            raise GraphValidationError(
                                f"property conflict at {kind} {key}, field {prop!r}; use a versioned ID or explicitly choose on_conflict='incoming'"
                            )
                        props[prop] = value
                    if props or kind == "node" or "props" in incoming:
                        incoming["props"] = props
                    if previous and canonical(previous) == canonical(incoming):
                        continue
                    self._put(kind, incoming)
                    changed_records.append((kind, previous, incoming))
                    changed += 1
                    if kind == "node" and incoming["label"] == "Chunk":
                        self._write_chunk(incoming)
                        written += 1
            if changed:
                self._set_meta("revision", int(base_revision) + 1)
        if replace:
            # Everything is new, so a bulk rebuild beats a quad-by-quad delta.
            self._ensure_rdf()
        elif changed:
            self._apply_rdf_delta(base_revision, removed_records, changed_records)
        return {
            **validation,
            "mode": "replace"
            if replace
            else "replace_source"
            if source is not None
            else "merge",
            "triples": len(self.store),
            "chunks_written": written,
            "chunks_total": self.content.execute(
                "SELECT COUNT(*) FROM chunks"
            ).fetchone()[0],
            "graph_store": str(self.graph_path),
            "content_store": str(self.content_path),
        }

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
        self, path: str | Path, max_bytes: int = MAX_PACKAGE_BYTES
    ) -> dict:
        """Atomically export portable neutral records in a ZIP container."""
        path = Path(path)
        if not path.name.endswith(PACKAGE_SUFFIX):
            path = path.with_name(path.name + PACKAGE_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        nodes, edges = self.export_records()
        payloads = {
            name: "".join(canonical(r) + "\n" for r in records).encode("utf-8")
            for name, records in (("nodes.jsonl", nodes), ("edges.jsonl", edges))
        }
        manifest = {
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION,
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
        }
        payloads["manifest.json"] = canonical(manifest).encode("utf-8")
        if sum(map(len, payloads.values())) > max_bytes:
            raise GraphValidationError(
                "package exceeds max_bytes; shard the corpus or explicitly raise the limit"
            )
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w+b") as stream:
                with zipfile.ZipFile(
                    stream, "w", zipfile.ZIP_DEFLATED, compresslevel=6
                ) as archive:
                    for name, data in payloads.items():
                        archive.writestr(name, data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {**manifest, "package": str(path), "bytes": path.stat().st_size}

    @staticmethod
    def read_package(path: str | Path, max_bytes: int = MAX_PACKAGE_BYTES):
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
                if len(names) != 3 or set(names) != PACKAGE_MEMBERS:
                    raise GraphValidationError(
                        "duplicate or unexpected archive members"
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
                if type(version) is not int or version not in (1, PACKAGE_VERSION):
                    raise GraphValidationError("unsupported package version")
                counts = manifest.get("counts")
                if not isinstance(counts, dict) or any(
                    type(counts.get(k)) is not int or counts[k] < 0
                    for k in ("nodes", "edges")
                ):
                    raise GraphValidationError("invalid manifest counts")
                payloads = {
                    name: archive.read(name) for name in ("nodes.jsonl", "edges.jsonl")
                }
                if version == 2:
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
                if version == 2:
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
    ) -> dict:
        manifest, nodes, edges = self.read_package(path, max_bytes=max_bytes)
        result = self.load(nodes, edges, replace=replace, on_conflict=on_conflict)
        return {
            **result,
            "package": str(path),
            "manifest": manifest,
            "integrity": "sha256"
            if manifest["version"] == 2
            else "legacy-no-checksums",
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
    ) -> dict:
        """Rank lexical evidence; an unmatched term is not proof of factual absence."""
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
        if not terms:
            return {"terms": [], "unmatched": [], "results": []}

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
            return {"terms": terms, "unmatched": unmatched, "results": []}

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
                    "document_id": bucket["hits"][hits[0]["node_iri"]]["document_id"]
                    if hits
                    else None,
                }
            )
        return {
            "terms": terms,
            "unmatched": unmatched,
            "results": results,
            "candidate_limit": candidate_limit,
            "capped_terms": capped,
        }

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
        sources = sorted(
            {
                row[0]
                for row in self.content.execute(
                    "SELECT DISTINCT repository FROM chunks"
                )
                if row[0]
            }
            | set(self.q("SELECT ?id WHERE { ?s a kg:Source ; kg:id ?id }"))
        )
        size = sum(p.stat().st_size for p in self.graph_path.rglob("*") if p.is_file())
        size += sum(
            p.stat().st_size
            for p in self.content_path.parent.glob(self.content_path.name + "*")
            if p.is_file()
        )
        return {
            "triples": len(self.store),
            "nodes": sum(labels.values()),
            "chunks": chunks,
            "sources": sources,
            "labels": dict(sorted(labels.items())),
            "rels": dict(sorted(rels.items())),
            "graph_store": str(self.graph_path),
            "content_store": str(self.content_path),
            "bytes": size,
        }
