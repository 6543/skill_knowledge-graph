"""Explicit size reduction: lossless first, then the coldest unprotected text."""

from __future__ import annotations

import pytest

import store
from store import KG, GraphValidationError

DAY = 20500


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(store, "usage_day", lambda: DAY)


def chunk(identifier, document, text, **props):
    return {
        "label": "Chunk",
        "id": identifier,
        "props": {
            "path": f"{document}.md",
            "source": "s",
            "document_id": document,
            "text": text,
            **props,
        },
    }


def has_chunk(document, identifier):
    return {
        "rel": "HAS_CHUNK",
        "src_label": "Document",
        "src": document,
        "dst_label": "Chunk",
        "dst": identifier,
    }


def corpus():
    nodes = [
        {"label": "Source", "id": "s", "props": {}},
        {"label": "Document", "id": "d1", "props": {"path": "d1.md", "source": "s"}},
        {
            "label": "Document",
            "id": "d0",
            "props": {"path": "d0.md", "source": "s", "outdated": True},
        },
        {
            "label": "Document",
            "id": "dk",
            "props": {"path": "dk.md", "source": "s", "outdated": True},
        },
        chunk("cold-big", "d1", "zebra " * 4000),
        chunk("cold-small", "d1", "yak " * 500),
        chunk("cited", "d1", "cited evidence " * 2000),
        chunk("read", "d1", "recently read " * 2000),
        chunk("old", "d0", "superseded version " * 2000),
        chunk("old-cited", "dk", "kept superseded " * 2000),
        {"label": "Lesson", "id": "l1", "props": {"statement": "x " * 3000}},
    ]
    edges = [
        has_chunk("d1", c) for c in ("cold-big", "cold-small", "cited", "read")
    ] + [
        has_chunk("d0", "old"),
        has_chunk("dk", "old-cited"),
        {
            "rel": "SUPPORTED_BY",
            "src_label": "Lesson",
            "src": "l1",
            "dst_label": "Chunk",
            "dst": "cited",
        },
        {
            "rel": "SUPPORTED_BY",
            "src_label": "Lesson",
            "src": "l1",
            "dst_label": "Chunk",
            "dst": "old-cited",
        },
    ]
    return nodes, edges


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    monkeypatch.delenv("KG_USAGE", raising=False)
    with KG("g", create=True) as kg:
        kg.load(*corpus())
        kg.record_use(reads=[("Chunk", "read")], hits=[("Chunk", "cold-small")] * 5)
    with KG("g") as kg:
        yield kg


def texts(kg):
    return {
        n["id"]: "text" in n["props"]
        for n in kg.export_records()[0]
        if n["label"] == "Chunk"
    }


def test_a_target_is_required(graph):
    with pytest.raises(GraphValidationError, match="target"):
        graph.compact()


def test_dry_run_plans_but_changes_nothing(graph):
    before = graph.export_records()
    report = graph.compact(target_bytes=10_000)
    assert report["dry_run"] is True
    assert report["levels"][1]["chunks"] >= 1
    assert graph.export_records() == before


def test_apply_evicts_coldest_unprotected_text_first_and_stops_at_target(graph):
    report = graph.compact(target_bytes=1, apply=True, checkpoint=False, lossless=False)
    assert report["levels"][1]["chunks"] == 1
    assert texts(graph) == {
        "cold-big": False,  # largest never-used text goes first
        "cold-small": True,
        "cited": True,
        "read": True,
        "old": True,
        "old-cited": True,
    }
    stub = graph.props("Chunk", "cold-big")
    assert stub["evicted"] == store.day_iso(DAY)
    assert len(stub["sha256"]) == 64 and stub["path"] == "d1.md"
    assert graph.search_keywords(["zebra"], scope="chunk") == []


def test_everything_unprotected_goes_but_memory_and_evidence_stay(graph):
    report = graph.compact(target_percent=99, apply=True, checkpoint=False)
    assert report["target_met"] is False
    remaining = texts(graph)
    assert remaining["cited"] and remaining["read"] and remaining["old-cited"]
    assert not remaining.get("cold-big") and not remaining.get("cold-small")
    assert graph.props("Lesson", "l1")["statement"].startswith("x x")
    assert report["protected"] == {"memory": 1, "evidence": 2, "recent_reads": 1}


def test_level_two_drops_cold_superseded_versions_nobody_cites(graph):
    graph.compact(target_percent=99, apply=True, checkpoint=False)
    assert not graph.exists("Document", "d0") and not graph.exists("Chunk", "old")
    assert graph.exists("Document", "dk") and graph.exists("Chunk", "old-cited")


def test_reingesting_the_same_text_restores_an_evicted_chunk(graph):
    graph.compact(target_bytes=1, apply=True, checkpoint=False, lossless=False)
    graph.load([chunk("cold-big", "d1", "zebra " * 4000)], [])
    props = graph.props("Chunk", "cold-big")
    assert props["text"].startswith("zebra") and "evicted" not in props
    assert graph.search_keywords(["zebra"], scope="chunk")


def test_checkpoint_is_written_before_lossy_steps(graph, tmp_path):
    report = graph.compact(target_bytes=1, apply=True, lossless=False)
    package = report["checkpoint"]["package"]
    with KG("restored", create=True) as restored:
        restored.import_package(package)
        assert restored.props("Chunk", "cold-big")["text"].startswith("zebra")


def test_lossless_step_alone_takes_no_checkpoint(graph):
    report = graph.compact(target_bytes=1, apply=True, lossless_only=True)
    assert report["checkpoint"] is None
    assert all(texts(graph).values())
