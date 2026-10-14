#!/usr/bin/env python3
"""Command-line interface for the hybrid knowledge graph store."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from store import KG, GraphValidationError, read_records, validate_graph, write_records


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

    load = command("load", help="load neutral records; replaces the graph by default")
    load.add_argument("--nodes", type=Path, required=True)
    load.add_argument("--edges", type=Path, required=True)
    mode = load.add_mutually_exclusive_group()
    mode.add_argument("--merge", action="store_true", help="add to what is already there")
    mode.add_argument("--source", help="replace only this source, keeping the others")

    validate = commands.add_parser("validate", help="check records before loading")
    validate.add_argument("--nodes", type=Path, required=True)
    validate.add_argument("--edges", type=Path, required=True)

    export = command("export", help="write a portable .graph-skill-db archive")
    export.add_argument("--out", type=Path, required=True)

    importer = command("import", help="merge a .graph-skill-db archive into a graph")
    importer.add_argument("--file", type=Path, required=True)
    importer.add_argument(
        "--replace", action="store_true", help="discard the current graph first"
    )

    inspect = commands.add_parser("inspect", help="read an archive without loading it")
    inspect.add_argument("--file", type=Path, required=True)

    dump = command("dump", help="write the graph back out as neutral records")
    dump.add_argument("--nodes", type=Path, required=True)
    dump.add_argument("--edges", type=Path, required=True)

    drop = command("drop-source", help="remove everything one source contributed")
    drop.add_argument("--source", required=True)

    command("stats", help="labels, relations, sources, and store size")

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

    query = command("query")
    group = query.add_mutually_exclusive_group(required=True)
    group.add_argument("--sparql")
    group.add_argument("--file", type=Path)
    return parser


def main():
    args = build_parser().parse_args()

    if args.command == "validate":
        return output(
            validate_graph(list(read_records(args.nodes)), list(read_records(args.edges)))
        )
    if args.command == "inspect":
        manifest, nodes, edges = KG.read_package(args.file)
        return output({**manifest, "read_nodes": len(nodes), "read_edges": len(edges)})

    creating = args.command in {"load", "import"}
    graph = KG(args.graph, create=creating)

    if args.command == "load":
        return output(
            graph.load(
                read_records(args.nodes),
                read_records(args.edges),
                replace=not args.merge,
                source=args.source,
            )
        )
    if args.command == "import":
        return output(graph.import_package(args.file, replace=args.replace))
    if args.command == "export":
        return output(graph.export_package(args.out))
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
            graph.neighbors(args.label, args.id, comma_list(args.rels), inbound=args.inbound)
        )
    if args.command == "reach":
        return output(graph.reach(args.label, args.id, comma_list(args.rels), args.node_label))
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
        if args.text:
            from store import tokenize

            return output(
                graph.search(
                    tokenize(args.text), limit=args.limit, scope=args.scope, source=args.source
                )
            )
        return output(
            graph.search(
                args.keyword,
                limit=args.limit,
                scope=args.scope,
                require_all=not args.any,
                source=args.source,
            )
        )
    if args.command == "query":
        sparql = args.sparql if args.sparql is not None else args.file.read_text()
        return output(graph.q(sparql))


if __name__ == "__main__":
    try:
        main()
    except (GraphValidationError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
