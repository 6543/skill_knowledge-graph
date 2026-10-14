"""Live usage counters: what commands returned (hits) and opened (reads)."""

from __future__ import annotations

import json
import zipfile

import kg as cli
import pytest
import store
from store import KG, GraphValidationError, usage_key

DAY = 20000


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(store, "usage_day", lambda: DAY)


def corpus():
    nodes = [
        {"label": "Source", "id": "docs", "props": {}},
        {
            "label": "Chunk",
            "id": "c1",
            "props": {
                "path": "a.md",
                "source": "docs",
                "text": "alpha timeout configuration",
            },
        },
        {
            "label": "Chunk",
            "id": "c2",
            "props": {
                "path": "b.md",
                "source": "docs",
                "text": "beta unrelated words " * 20,
            },
        },
        {"label": "Lesson", "id": "l1", "props": {"statement": "timeout lesson"}},
    ]
    edges = [
        {
            "rel": "SUPPORTED_BY",
            "src_label": "Lesson",
            "src": "l1",
            "dst_label": "Chunk",
            "dst": "c1",
        }
    ]
    return nodes, edges


def command(graph, *argv):
    args = cli.build_parser().parse_args([argv[0], graph.name, *argv[1:]])
    cli.run(args, graph)


def counts(graph):
    return {
        row[0]: (row[1], row[2])
        for row in graph.content.execute("SELECT h, hits, reads FROM usage")
    }


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    monkeypatch.delenv("KG_USAGE", raising=False)
    with KG("g", create=True) as graph:
        graph.load(*corpus())
    return tmp_path


def test_commands_count_hits_and_reads_once_per_command(home, capsys):
    graph = KG("g")
    command(graph, "search", "--text", "timeout")
    command(graph, "show", "--label", "Chunk", "--id", "c1")
    command(graph, "find", "--text", "timeout lesson")
    assert counts(graph) == {}, "buffered until the command's graph closes"
    graph.close()
    capsys.readouterr()

    with KG("g") as graph:
        used = counts(graph)
        assert used[usage_key("Chunk", "c1")] == (1, 1)
        assert used[usage_key("Lesson", "l1")][0] == 2  # search nodes + find
        assert usage_key("Chunk", "c2") not in used
        report = graph.usage_report()
    assert report["tracking"]["since"] == store.day_iso(DAY)
    assert report["labels"]["Chunk"] == {"nodes": 2, "used": 1, "hits": 1, "reads": 1}


def test_usage_report_and_export_do_not_count_as_use(home):
    with KG("g") as graph:
        graph.usage_report()
        graph.export_package(home / "p.graph-skill-db")
    with KG("g") as graph:
        assert counts(graph) == {}


def test_tracking_can_be_turned_off(home, monkeypatch, capsys):
    monkeypatch.setenv("KG_USAGE", "0")
    with KG("g") as graph:
        command(graph, "show", "--label", "Chunk", "--id", "c1")
    monkeypatch.delenv("KG_USAGE")
    with KG("g") as graph:
        assert counts(graph) == {}
        assert graph.usage_report()["tracking"]["enabled"] is True


def test_counters_halve_after_enough_events(home, monkeypatch):
    monkeypatch.setattr(store, "USAGE_HALVE_MIN", 8)
    monkeypatch.setattr(store, "USAGE_EVENTS_PER_NODE", 0)
    for _ in range(3):
        with KG("g") as graph:
            graph.record_use(hits=[("Chunk", "c1")] * 3, reads=[("Chunk", "c2")])
    with KG("g") as graph:
        used = counts(graph)
        tracking = graph.usage_report()["tracking"]
    # Events per session: 3 hits + 1 read. The second flush reaches 8 and halves
    # (6 -> 3 hits); the third adds 3 more, counted from zero again.
    assert tracking["generation"] == 1
    assert used[usage_key("Chunk", "c1")][0] == 6
    assert tracking["events_since_halving"] < 8


def test_halving_drops_rows_that_reach_zero(home, monkeypatch):
    monkeypatch.setattr(store, "USAGE_HALVE_MIN", 2)
    monkeypatch.setattr(store, "USAGE_EVENTS_PER_NODE", 0)
    with KG("g") as graph:
        graph.record_use(hits=[("Chunk", "c2")])
    with KG("g") as graph:
        graph.record_use(hits=[("Chunk", "c1")] * 4)
    with KG("g") as graph:
        assert usage_key("Chunk", "c2") not in counts(graph)


def test_usage_travels_in_packages_and_reimport_is_idempotent(home, tmp_path):
    with KG("g") as graph:
        graph.record_use(hits=[("Chunk", "c1")] * 3, reads=[("Lesson", "l1")])
    with KG("g") as graph:
        package = tmp_path / "p.graph-skill-db"
        report = graph.export_package(package)
    assert report["version"] == 4 and report["usage"]["rows"] == 2
    with zipfile.ZipFile(package) as archive:
        rows = [json.loads(line) for line in archive.read("usage.jsonl").splitlines()]
    assert {"label": "Chunk", "id": "c1", "hits": 3, "reads": 0, "day": DAY} in rows

    with KG("other", create=True) as other:
        other.import_package(package)
        other.import_package(package)
        assert counts(other)[usage_key("Chunk", "c1")] == (3, 0)


def test_import_merges_by_maximum_after_aligning_generations(
    home, tmp_path, monkeypatch
):
    with KG("g") as graph:
        graph.record_use(hits=[("Chunk", "c1")] * 8)
        graph.export_package(tmp_path / "p.graph-skill-db")
    with KG("other", create=True) as other:
        other.load(*corpus())
        other.record_use(hits=[("Chunk", "c1")] * 2, reads=[("Chunk", "c2")] * 4)
    with KG("other") as other:
        with other.content:
            other._set_meta("usage_generation", 1)
        other.import_package(tmp_path / "p.graph-skill-db")
        used = counts(other)
    # Incoming generation 0 is aligned to the local generation 1: 8 >> 1 = 4.
    assert used[usage_key("Chunk", "c1")] == (4, 0)
    assert used[usage_key("Chunk", "c2")] == (0, 4)


def test_packages_without_usage_stay_version_3(home, tmp_path):
    with KG("g") as graph:
        assert graph.export_package(tmp_path / "a.graph-skill-db")["version"] == 3
        graph.record_use(hits=[("Chunk", "c1")])
    with KG("g") as graph:
        report = graph.export_package(tmp_path / "b.graph-skill-db", usage=False)
    assert report["version"] == 3
    with zipfile.ZipFile(tmp_path / "b.graph-skill-db") as archive:
        assert "usage.jsonl" not in archive.namelist()


def test_tampered_usage_member_is_rejected(home, tmp_path):
    with KG("g") as graph:
        graph.record_use(hits=[("Chunk", "c1")])
    with KG("g") as graph:
        graph.export_package(tmp_path / "p.graph-skill-db")
    tampered = tmp_path / "t.graph-skill-db"
    with (
        zipfile.ZipFile(tmp_path / "p.graph-skill-db") as src,
        zipfile.ZipFile(tampered, "w") as dst,
    ):
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "usage.jsonl":
                data = data.replace(b'"hits":1', b'"hits":9')
            dst.writestr(item, data)
    with pytest.raises(GraphValidationError, match="usage.jsonl"):
        KG.read_package(tampered)


def test_dry_run_and_replace_imports(home, tmp_path):
    with KG("g") as graph:
        graph.record_use(hits=[("Chunk", "c1")])
    with KG("g") as graph:
        graph.export_package(tmp_path / "p.graph-skill-db")
    with KG("other", create=True) as other:
        other.load(*corpus())
        other.record_use(reads=[("Chunk", "c2")])
    with KG("other") as other:
        other.import_package(tmp_path / "p.graph-skill-db", dry_run=True)
        assert usage_key("Chunk", "c1") not in counts(other)
        other.import_package(tmp_path / "p.graph-skill-db", replace=True)
        assert counts(other) == {usage_key("Chunk", "c1"): (1, 0)}


def test_report_ranks_cold_text_by_source_and_document(home):
    with KG("g") as graph:
        graph.record_use(reads=[("Chunk", "c1")])
    with KG("g") as graph:
        report = graph.usage_report(top=5)
    text = report["chunk_text"]
    assert text["used_bytes"] == len("alpha timeout configuration")
    assert text["cold_bytes"] == len("beta unrelated words " * 20)
    assert report["cold_documents"][0]["path"] == "b.md"
    assert report["hot"][0] == {
        "label": "Chunk",
        "id": "c1",
        "hits": 0,
        "reads": 1,
        "last_used": store.day_iso(DAY),
    }
