"""CLI: batch delete from a file, refresh, diff, and memory-only import."""

from __future__ import annotations

import json

import pytest

import kg as cli
from ingest import build_graph
from store import KG, write_records


def run(graph_name, *argv, capsys):
    args = cli.build_parser().parse_args(list(argv))
    if args.command in {"diff"}:
        cli.standalone(args)
    else:
        graph = KG(graph_name, create=True)
        try:
            cli.run(args, graph)
        finally:
            graph.close()
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KG_USAGE", "0")
    with KG("g", create=True) as graph:
        graph.load(
            [{"label": "Thing", "id": f"t{i}", "props": {}} for i in range(500)],
            [
                {
                    "rel": "NEXT",
                    "src_label": "Thing",
                    "src": f"t{i}",
                    "dst_label": "Thing",
                    "dst": f"t{i + 1}",
                }
                for i in range(499)
            ],
        )
    return tmp_path


def test_delete_from_file_previews_then_removes_in_one_transaction(home, capsys):
    refs = home / "doomed.txt"
    lines = [f"Thing:t{i}" for i in range(400)]
    lines += [
        '{"label": "Thing", "id": "t450", "props": {}}',
        "",
        "# a comment",
        "Thing:nope",
    ]
    refs.write_text("\n".join(lines) + "\n")

    preview = run("g", "delete", "g", "--from", str(refs), capsys=capsys)
    assert preview["dry_run"] is True and preview["nodes_removed"] == 401
    assert preview["missing"] == ["Thing:nope"]

    with KG("g") as graph:
        revision = int(graph._meta("revision"))
        args = cli.build_parser().parse_args(
            ["delete", "g", "--from", str(refs), "--apply"]
        )
        cli.run(args, graph)
        assert int(graph._meta("revision")) == revision + 1  # one transaction
        assert not graph.exists("Thing", "t399") and graph.exists("Thing", "t400")
        assert not graph.exists("Thing", "t450")
    capsys.readouterr()


def test_delete_needs_node_or_from(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["delete", "g"])
    capsys.readouterr()


def test_refresh_and_diff_and_memory_only_from_the_cli(home, tmp_path, capsys):
    raw = tmp_path / "src"
    raw.mkdir()
    (raw / "a.md").write_text("# A\nfirst\n")
    nodes, edges = build_graph([raw], "src")
    write_records(tmp_path / "n.jsonl", nodes, jsonl=True)
    write_records(tmp_path / "e.jsonl", edges, jsonl=True)
    run(
        "s",
        "load",
        "s",
        "--nodes",
        str(tmp_path / "n.jsonl"),
        "--edges",
        str(tmp_path / "e.jsonl"),
        capsys=capsys,
    )
    run("s", "export", "s", "--out", str(tmp_path / "a"), capsys=capsys)

    (raw / "a.md").write_text("# A\nsecond\n")
    nodes, edges = build_graph([raw], "src")
    write_records(tmp_path / "n.jsonl", nodes, jsonl=True)
    write_records(tmp_path / "e.jsonl", edges, jsonl=True)
    report = run(
        "s",
        "refresh",
        "s",
        "--source",
        "src",
        "--nodes",
        str(tmp_path / "n.jsonl"),
        "--edges",
        str(tmp_path / "e.jsonl"),
        "--apply",
        capsys=capsys,
    )
    assert report["stale"] == 1 and report["deleted_documents"] == 1
    run("s", "export", "s", "--out", str(tmp_path / "b"), capsys=capsys)

    diff = run(
        None,
        "diff",
        "--file",
        str(tmp_path / "a.graph-skill-db"),
        "--file",
        str(tmp_path / "b.graph-skill-db"),
        capsys=capsys,
    )
    assert diff["nodes"]["only_a"] == {"Chunk": 1, "Document": 1}
    diff = run(
        None,
        "diff",
        "--graph",
        "s",
        "--file",
        str(tmp_path / "a.graph-skill-db"),
        capsys=capsys,
    )
    assert diff["nodes"]["only_b"] == {"Chunk": 1, "Document": 1}

    imported = run(
        "m",
        "import",
        "m",
        "--file",
        str(tmp_path / "a.graph-skill-db"),
        "--memory-only",
        capsys=capsys,
    )
    assert imported["memory_only"] == {
        "memory_nodes": 0,
        "evidence_nodes": 0,
        "edges": 0,
    }
