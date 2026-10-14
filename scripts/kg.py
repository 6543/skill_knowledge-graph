#!/usr/bin/env python3
"""Command-line interface for the hybrid knowledge graph store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from store import (
    KG,
    LARGE_BLOB_BYTES,
    MAX_PACKAGE_BYTES,
    MIN_SCORE,
    GraphValidationError,
    note_records,
    parse_ref,
    read_records,
    strict_json,
    tokenize,
    validate_graph,
    write_records,
)

LIST_PREVIEW = 20


def compact(value):
    """Shorten long lists and drop per-blob maps so output fits in context."""
    if isinstance(value, dict):
        return {key: compact(item) for key, item in value.items()}
    if isinstance(value, list) and len(value) > LIST_PREVIEW:
        return [compact(item) for item in value[:LIST_PREVIEW]] + [
            f"... {len(value) - LIST_PREVIEW} more (use --verbose)"
        ]
    if isinstance(value, list):
        return [compact(item) for item in value]
    return value


def manifest_summary(manifest: dict) -> dict:
    return {
        key: manifest.get(key)
        for key in ("graph", "version", "exported_at", "counts")
        if key in manifest
    } | {
        "sources": len(manifest.get("sources") or []),
        "blobs": len(manifest.get("blobs") or {}),
    }


def output(value):
    json.dump(value, sys.stdout, indent=2, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


def comma_list(value: str | None):
    return tuple(part for part in (value or "").split(",") if part)


def build_parser():
    parser = argparse.ArgumentParser(prog="kg.py")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, **kw):
        item = commands.add_parser(name, **kw)
        item.add_argument("graph")
        return item

    load = command("load", help="merge neutral records into the graph by default")
    load.add_argument("--nodes", type=Path, required=True)
    load.add_argument(
        "--edges", type=Path, help="optional; omit for a node-only delta batch"
    )
    mode = load.add_mutually_exclusive_group()
    mode.add_argument(
        "--merge", action="store_true", help="add to what is already there"
    )
    mode.add_argument(
        "--replace", action="store_true", help="explicitly replace the whole graph"
    )
    mode.add_argument("--source", help="replace only this source, keeping the others")
    load.add_argument("--on-conflict", choices=("error", "incoming"), default="error")
    load.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and every conflict; write nothing",
    )

    validate = commands.add_parser("validate", help="check records before loading")
    validate.add_argument("--nodes", type=Path, required=True)
    validate.add_argument("--edges", type=Path, required=True)

    export = command("export", help="write a portable .graph-skill-db archive")
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--max-bytes", type=int, default=MAX_PACKAGE_BYTES)
    export.add_argument(
        "--raw-root",
        type=Path,
        action="append",
        dest="raw_roots",
        default=[],
        help="ingest root holding the raw files behind Artifact records; repeatable",
    )
    export.add_argument(
        "--no-blobs",
        dest="blobs",
        action="store_false",
        help="records only; do not carry raw artifact bytes",
    )
    export.add_argument(
        "--large-bytes",
        type=int,
        default=LARGE_BLOB_BYTES,
        help="size above which an artifact is deferred instead of packed",
    )
    export.add_argument(
        "--include-large",
        action="store_true",
        help="pack deferred oversized artifacts; ask the user before using this",
    )

    importer = command("import", help="merge a .graph-skill-db archive into a graph")
    importer.add_argument("--file", type=Path, required=True)
    importer.add_argument(
        "--blob-dir",
        type=Path,
        help="unpack the package's raw artifact bytes into this object store",
    )
    importer.add_argument(
        "--on-conflict", choices=("error", "incoming"), default="error"
    )
    importer.add_argument("--max-bytes", type=int, default=MAX_PACKAGE_BYTES)
    importer.add_argument(
        "--replace", action="store_true", help="discard the current graph first"
    )
    importer.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change and every conflict; write nothing",
    )
    importer.add_argument(
        "--verbose", action="store_true", help="print the full manifest"
    )
    export.add_argument(
        "--verbose", action="store_true", help="print every path in the report"
    )

    inspect = commands.add_parser("inspect", help="read an archive without loading it")
    inspect.add_argument("--file", type=Path, required=True)
    inspect.add_argument("--max-bytes", type=int, default=MAX_PACKAGE_BYTES)

    dump = command("dump", help="write the graph back out as neutral records")
    dump.add_argument("--nodes", type=Path, required=True)
    dump.add_argument("--edges", type=Path, required=True)

    drop = command("drop-source", help="remove everything one source contributed")
    drop.add_argument("--source", required=True)

    command("stats", help="labels, relations, sources, and store size")

    brief = command("brief", help="session start: memory nodes newest first")
    brief.add_argument(
        "--label",
        action="append",
        help="list this label; repeatable (default: memory labels)",
    )
    brief.add_argument("--per-label", type=int, default=10)

    find = command("find", help="rank nodes by label, ID and properties")
    group = find.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="a natural-language question")
    group.add_argument("--keyword", action="append", help="an exact phrase; repeatable")
    find.add_argument("--label", action="append", help="only this label; repeatable")
    find.add_argument("--all", action="store_true", help="every phrase must match")
    find.add_argument("--source", help="only nodes owned by this source")
    find.add_argument("--limit", type=int, default=10)

    note = command("note", help="record a lesson, convention, finding ... in one step")
    note.add_argument("--text", required=True, help="the statement to keep")
    note.add_argument("--label", default="Lesson")
    note.add_argument("--id", help="default: <label>:<slug of text>")
    note.add_argument("--source", help="default: session-<UTC date>")
    note.add_argument(
        "--prop", action="append", default=[], help="KEY=VALUE, VALUE may be JSON"
    )
    note.add_argument(
        "--supported-by", action="append", default=[], help="LABEL:ID evidence"
    )
    note.add_argument("--about", action="append", default=[], help="LABEL:ID subject")
    note.add_argument(
        "--link", action="append", default=[], help="REL=LABEL:ID, any relation"
    )
    note.add_argument("--on-conflict", choices=("error", "incoming"), default="error")

    delete = command(
        "delete", help="remove nodes with their edges; previews by default"
    )
    delete.add_argument("--node", action="append", required=True, help="LABEL:ID")
    delete.add_argument("--apply", action="store_true", help="actually delete")

    prune = command(
        "prune", help="drop stale ingester mentions and orphans; previews by default"
    )
    prune.add_argument(
        "--orphan-label",
        action="append",
        help="labels whose edgeless nodes go (default: WebResource, EnvironmentVariable)",
    )
    prune.add_argument("--apply", action="store_true", help="actually delete")

    show = command("show", help="properties and neighbours of one node")
    show.add_argument("--label", required=True)
    show.add_argument("--id", required=True)

    neighbors = command("neighbors")
    neighbors.add_argument("--label", required=True)
    neighbors.add_argument("--id", required=True)
    neighbors.add_argument("--rels")
    neighbors.add_argument("--in", dest="inbound", action="store_true")

    reach = command("reach")
    reach.add_argument("--label", required=True)
    reach.add_argument("--id", required=True)
    reach.add_argument("--rels", required=True)
    reach.add_argument("--node-label")

    path = command("path")
    path.add_argument("--from-label", required=True)
    path.add_argument("--from-id", required=True)
    path.add_argument("--to-label", required=True)
    path.add_argument("--to-id", required=True)
    path.add_argument("--rels", required=True)
    path.add_argument("--max-hops", type=int, default=12)

    search = command("search")
    group = search.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="a natural-language question")
    group.add_argument("--keyword", action="append", help="an exact phrase; repeatable")
    search.add_argument("--scope", choices=("chunk", "file"), default="file")
    search.add_argument("--any", action="store_true", help="match any phrase, not all")
    search.add_argument("--source", help="restrict results to one source")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--candidate-limit", type=int, default=20000)
    search.add_argument(
        "--min-score",
        type=float,
        default=MIN_SCORE,
        help="drop results accounting for less than this share of the query",
    )
    search.add_argument(
        "--nodes",
        type=int,
        default=5,
        help="matching knowledge nodes to return beside chunks; 0 turns off",
    )
    search.add_argument(
        "--compact",
        action="store_true",
        help="evidence as id, path, lines and a 400-character snippet only",
    )

    query = command("query")
    group = query.add_mutually_exclusive_group(required=True)
    group.add_argument("--sparql")
    group.add_argument("--file", type=Path)
    return parser


def main():
    args = build_parser().parse_args()

    if args.command == "validate":
        return output(
            validate_graph(
                list(read_records(args.nodes)), list(read_records(args.edges))
            )
        )
    if args.command == "inspect":
        manifest, nodes, edges = KG.read_package(args.file, max_bytes=args.max_bytes)
        return output(
            {
                **manifest,
                "read_nodes": len(nodes),
                "read_edges": len(edges),
                "read_blobs": len(manifest.get("blobs") or {}),
            }
        )

    creating = args.command in {"load", "import", "note"}
    graph = KG(args.graph, create=creating)
    try:
        return run(args, graph)
    finally:
        graph.close()


def run(args, graph):

    if args.command == "load":
        return output(
            graph.load(
                read_records(args.nodes),
                read_records(args.edges) if args.edges else (),
                replace=args.replace,
                source=args.source,
                on_conflict=args.on_conflict,
                dry_run=args.dry_run,
            )
        )
    if args.command == "import":
        result = graph.import_package(
            args.file,
            replace=args.replace,
            on_conflict=args.on_conflict,
            max_bytes=args.max_bytes,
            blob_dir=args.blob_dir,
            dry_run=args.dry_run,
        )
        if not args.verbose:
            result = compact(
                {**result, "manifest": manifest_summary(result["manifest"])}
            )
        return output(result)
    if args.command == "export":
        result = graph.export_package(
            args.out,
            max_bytes=args.max_bytes,
            raw_roots=args.raw_roots,
            blobs=args.blobs,
            large_bytes=args.large_bytes,
            include_large=args.include_large,
        )
        return output(result if args.verbose else compact(result))
    if args.command == "brief":
        return output(graph.brief(args.label, per_label=args.per_label))
    if args.command == "find":
        return output(
            graph.find(
                args.text if args.text else args.keyword,
                labels=args.label,
                limit=args.limit,
                require_all=args.all,
                source=args.source,
            )
        )
    if args.command == "note":
        props = {}
        for item in args.prop:
            key, sep, value = item.partition("=")
            if not sep:
                raise GraphValidationError(f"--prop {item!r} must be KEY=VALUE")
            try:
                props[key] = strict_json(value)
            except ValueError:
                props[key] = value
        links = []
        for item in args.link:
            rel, sep, ref = item.partition("=")
            if not sep:
                raise GraphValidationError(f"--link {item!r} must be REL=LABEL:ID")
            links.append((rel, ref))
        nodes, edges = note_records(
            args.label,
            args.text,
            identifier=args.id,
            source=args.source,
            props=props,
            supported_by=args.supported_by,
            about=args.about,
            links=links,
        )
        for edge in edges:
            if not graph.exists(edge["dst_label"], edge["dst"]):
                hints = graph.suggest(edge["dst_label"], edge["dst"])
                raise GraphValidationError(
                    f"{edge['rel']} target {edge['dst_label']}:{edge['dst']} does not exist"
                    + (f"; did you mean {', '.join(hints)}?" if hints else "")
                )
        stored = graph.props(nodes[0]["label"], nodes[0]["id"])
        if stored.get("statement") == nodes[0]["props"]["statement"]:
            # Same knowledge seen again: keep its first date and session
            # instead of turning a repeat into a conflict.
            for key in ("observed", "source"):
                if (
                    key in stored
                    and key not in props
                    and not (key == "source" and args.source)
                ):
                    nodes[0]["props"].pop(key, None)
        result = graph.load(nodes, edges, on_conflict=args.on_conflict)
        return output(
            {
                "label": nodes[0]["label"],
                "id": nodes[0]["id"],
                "created": result["created"],
                "updated": result["updated"],
                "edges": len(edges),
            }
        )
    if args.command == "delete":
        return output(
            graph.delete(
                nodes=[parse_ref(ref) for ref in args.node], dry_run=not args.apply
            )
        )
    if args.command == "prune":
        return output(prune(graph, args.orphan_label, apply=args.apply))
    if args.command == "dump":
        nodes, edges = graph.export_records()
        return output(
            {
                "nodes": write_records(args.nodes, nodes),
                "edges": write_records(args.edges, edges),
            }
        )
    if args.command == "drop-source":
        return output(graph.drop_source(args.source))
    if args.command == "stats":
        return output(graph.stats())
    if args.command == "show":
        if not graph.exists(args.label, args.id):
            hints = graph.suggest(args.label, args.id)
            raise GraphValidationError(
                f"no node {args.label}:{args.id}"
                + (f"; did you mean {', '.join(hints)}?" if hints else "")
            )
        return output(
            {
                "label": args.label,
                "id": args.id,
                "props": graph.props(args.label, args.id),
                "out": graph.neighbors(args.label, args.id),
                "in": graph.neighbors(args.label, args.id, inbound=True),
            }
        )
    if args.command == "neighbors":
        return output(
            graph.neighbors(
                args.label, args.id, comma_list(args.rels), inbound=args.inbound
            )
        )
    if args.command == "reach":
        return output(
            graph.reach(args.label, args.id, comma_list(args.rels), args.node_label)
        )
    if args.command == "path":
        return output(
            graph.path(
                args.from_label,
                args.from_id,
                args.to_label,
                args.to_id,
                comma_list(args.rels),
                args.max_hops,
            )
        )
    if args.command == "search":
        return output(
            graph.search(
                tokenize(args.text) if args.text else args.keyword,
                limit=args.limit,
                scope=args.scope,
                require_all=bool(args.keyword) and not args.any,
                min_score=args.min_score,
                source=args.source,
                candidate_limit=args.candidate_limit,
                nodes=args.nodes,
            )
            if not args.compact
            else compact_search(
                graph.search(
                    tokenize(args.text) if args.text else args.keyword,
                    limit=args.limit,
                    scope=args.scope,
                    require_all=bool(args.keyword) and not args.any,
                    min_score=args.min_score,
                    source=args.source,
                    candidate_limit=args.candidate_limit,
                    nodes=args.nodes,
                )
            )
        )
    if args.command == "query":
        sparql = args.sparql if args.sparql is not None else args.file.read_text()
        return output(graph.q(sparql))


def compact_search(result: dict, width: int = 400) -> dict:
    keep = ("id", "path", "heading", "start_line", "end_line")
    for item in result.get("results", []):
        item["evidence"] = [
            {key: evidence.get(key) for key in keep}
            | {"text": evidence.get("text", "")[:width]}
            for evidence in item.get("evidence", [])
        ]
    return result


MENTION_RELS = {"LINKS_TO": "WebResource", "MENTIONS_ENV": "EnvironmentVariable"}


def prune(graph, orphan_labels=None, apply: bool = False) -> dict:
    """Remove mention edges the current ingester would not create, then orphans.

    Merging an older package brings back mention edges from a buggier ingester
    (URLs with trailing backticks, "http://." ...). Only prop-less LINKS_TO and
    MENTIONS_ENV edges from chunks the bundled ingester extracted are judged;
    domain edges are never touched.
    """
    from ingest import ENVIRONMENT_VARIABLE, url_mentions

    expected = {}
    stale = []
    for edge in graph.export_records()[1]:
        target = MENTION_RELS.get(edge["rel"])
        if (
            target is None
            or edge.get("props")
            or edge["src_label"] != "Chunk"
            or edge["dst_label"] != target
        ):
            continue
        if edge["src"] not in expected:
            props = graph.props("Chunk", edge["src"])
            extractor = str(props.get("extractor", ""))
            text = props.get("text")
            if not extractor.startswith(
                ("plain-text@", "html-text@")
            ) or not isinstance(text, str):
                expected[edge["src"]] = None
            else:
                expected[edge["src"]] = {
                    "LINKS_TO": set(url_mentions(text)),
                    "MENTIONS_ENV": set(ENVIRONMENT_VARIABLE.findall(text)),
                }
        wanted = expected[edge["src"]]
        if wanted is not None and edge["dst"] not in wanted[edge["rel"]]:
            stale.append(edge)
    labels = orphan_labels or sorted(set(MENTION_RELS.values()))
    stale_keys = {graph._key("edge", edge) for edge in stale}
    # A node becomes an orphan once its only edges are the stale ones.
    remaining = set()
    for edge in graph.export_records()[1]:
        if graph._key("edge", edge) in stale_keys:
            continue
        remaining.add((edge["src_label"], edge["src"]))
        remaining.add((edge["dst_label"], edge["dst"]))
    orphans = [
        (label, identifier)
        for label in labels
        for identifier in graph.q(
            f"SELECT ?id WHERE {{ ?n a kg:{label} ; kg:id ?id }} ORDER BY ?id"
        )
        if (label, identifier) not in remaining
    ]
    report = {
        "dry_run": not apply,
        "stale_mentions": len(stale),
        "orphans": len(orphans),
        "examples": sorted({edge["dst"] for edge in stale})[:LIST_PREVIEW],
    }
    if apply and (stale or orphans):
        graph.delete(nodes=orphans, edges=stale)
    return report


if __name__ == "__main__":
    try:
        main()
    except (GraphValidationError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
