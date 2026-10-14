#!/usr/bin/env python3
"""Persistent RDF topology plus SQLite FTS content for knowledge graphs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

import pyoxigraph as ox

DEFAULT_NS = os.environ.get("KG_NAMESPACE", "urn:kg:")
RDF_TYPE = ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
XSD = "http://www.w3.org/2001/XMLSchema#"
SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*")

PACKAGE_SUFFIX = ".graph-skill-db"
PACKAGE_FORMAT = "graph-skill-db"
PACKAGE_VERSION = 1

# A term found in a path or heading is scarcer than the same term in a body, and it
# says what a file is *for* rather than what it happens to mention. Weighting it
# above body evidence is what stops a huge changelog or translation file from
# burying the one module that actually implements the thing being asked about.
PATH_WEIGHT = 1.5
# Below this share of a question's information mass, a hit is noise rather than evidence.
MIN_SCORE = 0.4

STOP_WORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "being", "between", "both", "but", "by", "can",
    "could", "did", "do", "does", "each", "find", "for", "from", "get", "give",
    "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its",
    "list", "make", "many", "me", "much", "must", "my", "no", "not", "of", "on",
    "one", "or", "our", "out", "over", "should", "show", "so", "some", "such",
    "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "this", "those", "through", "to", "up", "use", "used", "using", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "why", "will",
    "with", "would", "you", "your",
}


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
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise GraphValidationError(f"{path}:{line_number}: {exc}") from exc
        return
    value = json.loads(path.read_text(encoding="utf-8"))
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
                stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
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
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
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
            raise GraphValidationError(f"{owner}: 'id' is reserved; rename the property")
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
        digest = json.dumps(props, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        key = (label, identifier)
        if key in known and known[key] != digest:
            raise GraphValidationError(f"conflicting duplicate node {label}:{identifier}")
        known[key] = digest
        node_count += 1

    edge_count = 0
    resolved = 0
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise GraphValidationError(f"edge {index}: expected an object")
        rel = _check_name("relation", edge.get("rel"))
        src_label = _check_name("label", edge.get("src_label"))
        dst_label = _check_name("label", edge.get("dst_label"))
        _check_props(f"edge {rel}", edge.get("props"))
        for role, endpoint in (
            ("source", (src_label, edge.get("src"))),
            ("destination", (dst_label, edge.get("dst"))),
        ):
            if endpoint in known:
                continue
            if resolve is not None and isinstance(endpoint[1], str) and resolve(*endpoint):
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
    if isinstance(value, bool):
        return ox.Literal("true" if value else "false", datatype=ox.NamedNode(XSD + "boolean"))
    if isinstance(value, int):
        return ox.Literal(str(value), datatype=ox.NamedNode(XSD + "integer"))
    if isinstance(value, float):
        return ox.Literal(repr(value), datatype=ox.NamedNode(XSD + "double"))
    if isinstance(value, (list, dict)):
        return ox.Literal(json.dumps(value, sort_keys=True, ensure_ascii=False))
    return ox.Literal(str(value))


def _term_value(term):
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
    def __init__(self, name: str, ns: str = DEFAULT_NS, create: bool = False):
        self.name = name
        self.ns = ns
        self.home = graph_home()
        self.graph_path = self.home / "rdf" / f"{name}.oxi"
        self.content_path = self.home / "content" / f"{name}.sqlite3"
        self.file = self.graph_path
        if not self.graph_path.exists() and not create:
            raise FileNotFoundError(f"no graph {name!r} at {self.graph_path}")
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.content_path.parent.mkdir(parents=True, exist_ok=True)
        # SQLite opens before Oxigraph; some embedded-store combinations interfere otherwise.
        self.content = sqlite3.connect(self.content_path)
        self.content.row_factory = sqlite3.Row
        self._prepare_content()
        self.store = ox.Store(str(self.graph_path))

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
            PRAGMA synchronous=NORMAL;
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
                sha256 TEXT NOT NULL
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
        self.content.commit()

    def node_iri(self, label: str, identifier: str) -> ox.NamedNode:
        return ox.NamedNode(f"{self.ns}{label}/{quote(str(identifier), safe='')}")

    def edge_iri(self, edge: dict) -> ox.NamedNode:
        identity = {key: edge[key] for key in ("rel", "src_label", "src", "dst_label", "dst")}
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
        return any(self.store.quads_for_pattern(self.node_iri(label, identifier), RDF_TYPE, None))

    def _extend(self, quads: list, batch_size: int = 10_000) -> None:
        for offset in range(0, len(quads), batch_size):
            self.store.extend(quads[offset : offset + batch_size])

    # ------------------------------------------------------------------- write
    def clear(self) -> None:
        self.store.update("CLEAR ALL")
        with self.content:
            self.content.execute("DELETE FROM chunks")
            self.content.execute("DELETE FROM locations_fts")
            self.content.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

    def _source_subjects(self, source: str):
        value = ox.Literal(source)
        seen = set()
        for predicate in ("source", "repository"):
            for subject, _, _, _ in self.store.quads_for_pattern(
                None, ox.NamedNode(self.ns + predicate), value
            ):
                if subject not in seen:
                    seen.add(subject)
        marker = self.node_iri("Source", source)
        if marker not in seen and any(self.store.quads_for_pattern(marker, None, None)):
            seen.add(marker)
        return seen

    def drop_source(self, source: str) -> dict:
        """Remove everything one source contributed, leaving the other sources intact."""
        subjects = self._source_subjects(source)
        removed = 0
        for subject in subjects:
            for quad in list(self.store.quads_for_pattern(subject, None, None)):
                self.store.remove(quad)
                removed += 1
            for quad in list(self.store.quads_for_pattern(None, None, subject)):
                self.store.remove(quad)
                removed += 1
        with self.content:
            rows = self.content.execute(
                "SELECT rowid FROM chunks WHERE repository = ?", (source,)
            ).fetchall()
            self.content.executemany(
                "DELETE FROM locations_fts WHERE rowid = ?", [(r["rowid"],) for r in rows]
            )
            self.content.execute("DELETE FROM chunks WHERE repository = ?", (source,))
            self.content.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        return {"source": source, "subjects_removed": len(subjects), "quads_removed": removed}

    def _clear_node_properties(self, subjects) -> None:
        """Drop literal quads so a merge upserts values instead of stacking them."""
        for subject in subjects:
            for quad in list(self.store.quads_for_pattern(subject, None, None)):
                if not isinstance(quad.object, ox.NamedNode):
                    self.store.remove(quad)

    def load(self, nodes, edges, replace: bool = True, source: str | None = None) -> dict:
        """Load neutral records.

        `replace` clears the graph first, because material removed from a corpus
        must also remove its stale facts. `source` narrows that reset to one
        source, so refreshing one corpus never discards the others. With
        `replace=False` the records are merged: node properties are upserted and
        new facts are added to what is already known.
        """
        nodes = list(nodes)
        edges = list(edges)
        merging = not replace
        validation = validate_graph(nodes, edges, resolve=self.exists if merging else None)
        if replace:
            self.drop_source(source) if source else self.clear()

        quads: list = []
        chunk_rows: list = []
        seen_nodes: set = set()
        subjects: list = []
        for node in nodes:
            key = (node["label"], node["id"])
            if key in seen_nodes:
                continue
            seen_nodes.add(key)
            subject = self.node_iri(*key)
            subjects.append(subject)
            quads.append(ox.Quad(subject, RDF_TYPE, ox.NamedNode(self.ns + node["label"])))
            quads.append(ox.Quad(subject, ox.NamedNode(self.ns + "id"), ox.Literal(node["id"])))
            props = node.get("props") or {}
            text = props.get("text") if node["label"] == "Chunk" else None
            for prop, value in sorted(props.items()):
                if value in (None, "") or (node["label"] == "Chunk" and prop == "text"):
                    continue
                quads.append(ox.Quad(subject, ox.NamedNode(self.ns + prop), literal(value)))
            if text not in (None, ""):
                repository = str(props.get("repository") or props.get("source") or "")
                path = str(props.get("path") or node["id"])
                document_id = str(props.get("document_id") or f"{repository}:{path}")
                digest = str(
                    props.get("sha256") or hashlib.sha256(str(text).encode()).hexdigest()
                )
                chunk_rows.append(
                    (
                        subject.value,
                        node["id"],
                        repository,
                        path,
                        document_id,
                        str(props.get("heading") or ""),
                        str(text),
                        props.get("start_line"),
                        props.get("end_line"),
                        digest,
                    )
                )

        edge_facts = []
        for edge in edges:
            subject = self.node_iri(edge["src_label"], edge["src"])
            predicate = ox.NamedNode(self.ns + edge["rel"])
            obj = self.node_iri(edge["dst_label"], edge["dst"])
            quads.append(ox.Quad(subject, predicate, obj))
            if edge.get("props"):
                fact = self.edge_iri(edge)
                edge_facts.append(fact)
                quads.extend(
                    [
                        ox.Quad(fact, ox.NamedNode(self.ns + "_subject"), subject),
                        ox.Quad(fact, ox.NamedNode(self.ns + "_predicate"), predicate),
                        ox.Quad(fact, ox.NamedNode(self.ns + "_object"), obj),
                    ]
                )
                for prop, value in sorted(edge["props"].items()):
                    if value not in (None, ""):
                        quads.append(
                            ox.Quad(
                                fact,
                                ox.NamedNode(self.ns + "_edge_prop_" + prop),
                                literal(value),
                            )
                        )

        if merging:
            self._clear_node_properties(subjects)
            self._clear_node_properties(edge_facts)

        self._extend(quads)
        self._write_chunks(chunk_rows)
        mode = "merge" if merging else ("replace_source" if source else "replace")
        return {
            **validation,
            "mode": mode,
            "triples": len(self.store),
            "chunks_written": len(chunk_rows),
            "chunks_total": self.content.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "graph_store": str(self.graph_path),
            "content_store": str(self.content_path),
        }

    def _write_chunks(self, rows) -> None:
        if not rows:
            return
        with self.content:
            self.content.executemany(
                """INSERT INTO chunks
                (node_iri,id,repository,path,document_id,heading,text,start_line,end_line,sha256)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(node_iri) DO UPDATE SET
                  id=excluded.id, repository=excluded.repository, path=excluded.path,
                  document_id=excluded.document_id, heading=excluded.heading,
                  text=excluded.text, start_line=excluded.start_line,
                  end_line=excluded.end_line, sha256=excluded.sha256""",
                rows,
            )
            self.content.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            self.content.execute("DELETE FROM locations_fts")
            self.content.executemany(
                "INSERT INTO locations_fts(rowid, loc) VALUES (?,?)",
                [
                    (row["rowid"], self._location_text(row["path"], row["heading"]))
                    for row in self.content.execute("SELECT rowid, path, heading FROM chunks")
                ],
            )

    @staticmethod
    def _location_text(path: str, heading: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", " ", f"{path or ''} {heading or ''}").strip()

    # ---------------------------------------------------------------- transfer
    def export_records(self):
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

        for subject, predicate, obj, _ in self.store.quads_for_pattern(None, None, None):
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
            nodes.setdefault(subject, {"label": label, "id": self.decode(subject), "props": {}})
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
            key=lambda r: (r["rel"], r["src_label"], r["src"], r["dst_label"], r["dst"]),
        )
        return node_list, edge_list

    def export_package(self, path: str | Path) -> dict:
        """Write a portable `.graph-skill-db` archive: a zip of the neutral records."""
        path = Path(path)
        if not path.name.endswith(PACKAGE_SUFFIX):
            path = path.with_name(path.name + PACKAGE_SUFFIX)
        path.parent.mkdir(parents=True, exist_ok=True)
        nodes, edges = self.export_records()
        manifest = {
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION,
            "graph": self.name,
            "namespace": self.ns,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": {"nodes": len(nodes), "edges": len(edges)},
            "labels": dict(sorted(Counter(n["label"] for n in nodes).items())),
            "rels": dict(sorted(Counter(e["rel"] for e in edges).items())),
            "sources": sorted(n["id"] for n in nodes if n["label"] == "Source"),
        }
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            for name, records in (("nodes.jsonl", nodes), ("edges.jsonl", edges)):
                archive.writestr(
                    name,
                    "".join(
                        json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in records
                    ),
                )
        return {**manifest, "package": str(path), "bytes": path.stat().st_size}

    @staticmethod
    def read_package(path: str | Path):
        path = Path(path)
        if not zipfile.is_zipfile(path):
            raise GraphValidationError(f"{path}: not a zip archive")
        with zipfile.ZipFile(path) as archive:
            missing = {"manifest.json", "nodes.jsonl", "edges.jsonl"} - set(archive.namelist())
            if missing:
                raise GraphValidationError(f"{path}: archive is missing {sorted(missing)}")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != PACKAGE_FORMAT:
                raise GraphValidationError(f"{path}: not a {PACKAGE_FORMAT} archive")
            if int(manifest.get("version", 1)) > PACKAGE_VERSION:
                raise GraphValidationError(
                    f"{path}: package version {manifest['version']} is newer than this skill supports"
                )

            def records(name):
                out = []
                for line_number, line in enumerate(
                    archive.read(name).decode("utf-8").splitlines(), 1
                ):
                    if line.strip():
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError as exc:
                            raise GraphValidationError(
                                f"{path}:{name}:{line_number}: {exc}"
                            ) from exc
                return out

            return manifest, records("nodes.jsonl"), records("edges.jsonl")

    def import_package(self, path: str | Path, replace: bool = False) -> dict:
        """Merge an exported archive into this graph.

        Merging is the default: an import adds to what the graph already knows
        rather than discarding it.
        """
        manifest, nodes, edges = self.read_package(path)
        result = self.load(nodes, edges, replace=replace)
        return {**result, "package": str(path), "manifest": manifest}

    # -------------------------------------------------------------------- read
    def q(self, sparql: str):
        result = self.store.query(self.prefixes + sparql)
        if isinstance(result, ox.QueryBoolean):
            return bool(result)
        rows = []
        for row in result:
            values = [_term_value(value) for value in row]
            rows.append(values[0] if len(values) == 1 else values)
        return rows

    def props(self, label: str, identifier: str) -> dict:
        result = {}
        for _, predicate, obj, _ in self.store.quads_for_pattern(
            self.node_iri(label, identifier), None, None
        ):
            if isinstance(obj, ox.NamedNode):
                continue
            result[predicate.value.removeprefix(self.ns)] = _term_value(obj)
        if label == "Chunk":
            row = self.content.execute(
                "SELECT text FROM chunks WHERE node_iri = ?",
                (self.node_iri(label, identifier).value,),
            ).fetchone()
            if row:
                result["text"] = row["text"]
        return result

    def edge_props(self, rel, src_label, src, dst_label, dst) -> dict:
        fact = self.edge_iri(
            {"rel": rel, "src_label": src_label, "src": src, "dst_label": dst_label, "dst": dst}
        )
        prefix = self.ns + "_edge_prop_"
        return {
            predicate.value.removeprefix(prefix): _term_value(obj)
            for _, predicate, obj, _ in self.store.quads_for_pattern(fact, None, None)
            if predicate.value.startswith(prefix)
        }

    def neighbors(self, label: str, identifier: str, rels=(), inbound: bool = False) -> list[dict]:
        target = self.node_iri(label, identifier)
        pattern = (None, None, target) if inbound else (target, None, None)
        result = []
        internal = {"_subject", "_predicate", "_object"}
        for subject, predicate, obj, _ in self.store.quads_for_pattern(*pattern):
            rel = predicate.value.removeprefix(self.ns)
            other = subject if inbound else obj
            if predicate == RDF_TYPE or not isinstance(other, ox.NamedNode) or rel in internal:
                continue
            if rels and rel not in rels:
                continue
            other_type = next(
                (
                    value.value.removeprefix(self.ns)
                    for _, _, value, _ in self.store.quads_for_pattern(other, RDF_TYPE, None)
                ),
                None,
            )
            result.append({"rel": rel, "label": other_type, "id": self.decode(other)})
        return sorted(result, key=lambda row: (row["rel"], row["label"] or "", row["id"]))

    def reach(self, label, identifier, rels, node_label=None) -> list[str]:
        if not rels:
            raise ValueError("reach requires at least one relation")
        for rel in rels:
            _check_name("relation", rel)
        relation_path = "|".join(f"kg:{rel}" for rel in rels)
        label_pattern = f"?target a kg:{_check_name('label', node_label)} ." if node_label else ""
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
        for rel in rels:
            _check_name("relation", rel)
        relation_path = "|".join(f"kg:{rel}" for rel in rels)
        start = self.iri(start_label, start_id)
        end = self.iri(end_label, end_id)
        # One transitive ASK is far cheaper than deepening to max_hops on an unreachable pair.
        if not self.q(f"ASK {{ {start} ({relation_path})+ {end} }}"):
            return []
        for hops in range(1, max_hops + 1):
            patterns, ids, previous = [], [], start
            for index in range(1, hops + 1):
                current, identifier = f"?node{index}", f"?id{index}"
                patterns.append(f"{previous} ({relation_path}) {current} .")
                patterns.append(f"{current} kg:id {identifier} .")
                ids.append(identifier)
                previous = current
            patterns.append(f"FILTER(?node{hops} = {end})")
            rows = self.q(f"SELECT {' '.join(ids)} WHERE {{ {' '.join(patterns)} }} LIMIT 1")
            if rows:
                tail = rows[0] if isinstance(rows[0], list) else [rows[0]]
                return [start_id, *tail]
        return []

    # ------------------------------------------------------------------ search
    @staticmethod
    def _fts_phrase(term: str) -> str:
        return '"' + term.replace('"', '""') + '"'

    def _hits(self, term: str, table: str):
        columns = """c.rowid, c.node_iri, c.id, c.repository, c.path, c.document_id,
                     c.heading, c.text, c.start_line, c.end_line"""
        if table == "text":
            sql = f"""SELECT {columns}, bm25(chunks_fts) AS rank
                      FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid
                      WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 20000"""
        else:
            sql = f"""SELECT {columns}, bm25(locations_fts) AS rank
                      FROM locations_fts JOIN chunks c ON c.rowid = locations_fts.rowid
                      WHERE locations_fts MATCH ? ORDER BY rank LIMIT 20000"""
        # Paths and headings are terse, so a prefix match there recovers the common
        # "archive" / "archiver", "migrate" / "migrations" near misses that exact
        # tokenisation drops. Bodies stay exact, where prefix matching adds noise.
        phrase = self._fts_phrase(term) + ("*" if table == "location" else "")
        try:
            return self.content.execute(sql, (phrase,)).fetchall()
        except sqlite3.OperationalError:
            return []

    def search(
        self,
        terms,
        limit: int = 10,
        scope: str = "file",
        require_all: bool = False,
        min_score: float = MIN_SCORE,
        source: str | None = None,
    ) -> dict:
        """Rank evidence by how much of a question's information a file accounts for.

        Rare terms weigh more than common ones, a file scores on its best matching
        chunk rather than the sum over all of them (otherwise the largest file in
        the corpus always wins), and a match in a path or heading counts extra.
        The returned `unmatched` list is the useful negative signal: a term that
        appears nowhere means the fact is absent, not merely badly ranked.
        """
        terms = [t for t in dict.fromkeys((t or "").strip() for t in terms or []) if t]
        if not terms:
            return {"terms": [], "unmatched": [], "results": []}

        text_hits = {term: self._hits(term, "text") for term in terms}
        location_hits = {term: self._hits(term, "location") for term in terms}
        # A term that only ever appears in a path still identifies a file, so it
        # counts as matched; only a term found nowhere at all is absent.
        matched = [term for term in terms if text_hits[term] or location_hits[term]]
        unmatched = [term for term in terms if not (text_hits[term] or location_hits[term])]
        if not matched or (require_all and unmatched):
            return {"terms": terms, "unmatched": unmatched, "results": []}

        total_chunks = self.content.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] or 1
        idf = {
            term: math.log(
                1 + total_chunks / (len(text_hits[term]) or len(location_hits[term]))
            )
            for term in matched
        }
        weight = sum(idf.values()) or 1.0
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
                bucket_for(row)["location_terms"].add(term)

        scored = []
        for bucket in buckets.values():
            if require_all and len(bucket["terms"] | bucket["location_terms"]) != len(matched):
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
            evidence = [
                {
                    "id": row["id"],
                    "heading": row["heading"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "text": row["text"][:1200],
                    "truncated": len(row["text"]) > 1200,
                }
                for row in sorted(
                    bucket["hits"].values(),
                    key=lambda hit: (hit["start_line"] or 0, hit["id"]),
                )[:8]
            ]
            results.append(
                {
                    "repository": bucket["repository"],
                    "path": bucket["path"],
                    "score": round(score, 3),
                    "coverage": round(covered, 3),
                    "matched_terms": sorted(bucket["terms"] | bucket["location_terms"]),
                    "evidence": evidence,
                }
            )
        return {"terms": terms, "unmatched": unmatched, "results": results}

    def search_keywords(self, keywords, limit=10, scope="file", require_all=True, source=None):
        return self.search(
            keywords, limit=limit, scope=scope, require_all=require_all, source=source
        )["results"]

    def search_question(self, question: str, limit: int = 10, source: str | None = None):
        return self.search(tokenize(question), limit=limit, scope="file", source=source)["results"]

    # ------------------------------------------------------------------- stats
    def stats(self) -> dict:
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
        sources = [
            row[0]
            for row in self.content.execute(
                "SELECT DISTINCT repository FROM chunks ORDER BY repository"
            )
        ]
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
