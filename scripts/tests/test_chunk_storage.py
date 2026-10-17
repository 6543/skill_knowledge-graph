"""Chunk text is stored once: in the chunks table, not again inside the record JSON."""

from __future__ import annotations

import json

import pytest

from store import KG, GraphValidationError, canonical


def chunk(identifier, text=None, **props):
    props = {"path": "a.md", "source": "s", **props}
    if text is not None:
        props["text"] = text
    return {"label": "Chunk", "id": identifier, "props": props}


def stored_record(graph, label, identifier):
    row = graph.content.execute(
        "SELECT value FROM records WHERE kind='node' AND key=?",
        (canonical([label, identifier]),),
    ).fetchone()
    return json.loads(row[0])


@pytest.fixture
def graph(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("dedup", create=True) as kg:
        yield kg


def test_record_json_omits_chunk_text_but_every_reader_still_sees_it(graph):
    graph.load([chunk("c1", "  indented\nbody  ")], [])

    assert "text" not in stored_record(graph, "Chunk", "c1")["props"]
    assert graph.props("Chunk", "c1")["text"] == "  indented\nbody  "
    nodes, _ = graph.export_records()
    assert nodes[0]["props"]["text"] == "  indented\nbody  "
    assert graph.search_keywords(["indented"], scope="chunk")


def test_chunk_without_text_stays_without_text(graph):
    graph.load([chunk("bare")], [])
    assert "text" not in graph.props("Chunk", "bare")
    assert "text" not in graph.export_records()[0][0]["props"]


def test_reloading_identical_chunk_is_a_noop_and_changed_text_conflicts(graph):
    graph.load([chunk("c1", "one")], [])
    assert graph.load([chunk("c1", "one")], [])["created"] == 0
    with pytest.raises(GraphValidationError, match="text"):
        graph.load([chunk("c1", "two")], [])
    assert graph.props("Chunk", "c1")["text"] == "one"


def test_incoming_correction_replaces_text_once(graph):
    graph.load([chunk("c1", "one")], [])
    graph.load([chunk("c1", "two")], [], on_conflict="incoming")
    assert graph.props("Chunk", "c1")["text"] == "two"
    assert "text" not in stored_record(graph, "Chunk", "c1")["props"]
    assert graph.search_keywords(["two"], scope="chunk")
    assert not graph.search_keywords(["one"], scope="chunk")


def test_older_graph_with_inline_text_is_migrated_on_open(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("legacy", create=True) as kg:
        kg.load([chunk("c1", "legacy body"), chunk("bare")], [])
        expected = kg.export_records()
        # Recreate the earlier layout: text inline in the record JSON.
        record = stored_record(kg, "Chunk", "c1")
        record["props"]["text"] = "legacy body"
        with kg.content:
            kg.content.execute(
                "UPDATE records SET value=? WHERE kind='node' AND key=?",
                (canonical(record), canonical(["Chunk", "c1"])),
            )
            kg.content.execute("DELETE FROM metadata WHERE key='chunk_text_store'")

    with KG("legacy") as kg:
        assert "text" not in stored_record(kg, "Chunk", "c1")["props"]
        assert kg.export_records() == expected


def test_migration_keeps_inline_text_that_disagrees_with_the_index(
    tmp_path, monkeypatch
):
    """Never discard the authoritative copy when the two stores disagree."""
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("legacy", create=True) as kg:
        kg.load([chunk("c1", "authoritative")], [])
        record = stored_record(kg, "Chunk", "c1")
        record["props"]["text"] = "authoritative"
        with kg.content:
            kg.content.execute(
                "UPDATE records SET value=? WHERE kind='node' AND key=?",
                (canonical(record), canonical(["Chunk", "c1"])),
            )
            kg.content.execute("UPDATE chunks SET text='stale'")
            kg.content.execute("DELETE FROM metadata WHERE key='chunk_text_store'")

    with KG("legacy") as kg:
        assert kg.props("Chunk", "c1")["text"] == "authoritative"


def test_deleting_and_dropping_chunks_still_works(graph):
    graph.load([chunk("c1", "x"), chunk("c2", "y", source="t")], [])
    graph.delete(nodes=[("Chunk", "c1")])
    graph.drop_source("t")
    assert graph.export_records() == ([], [])
    assert graph.content.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_package_round_trip_keeps_text(graph, tmp_path):
    graph.load([chunk("c1", "travels")], [])
    package = tmp_path / "p.graph-skill-db"
    graph.export_package(package)
    with KG("other", create=True) as other:
        other.import_package(package)
        assert other.props("Chunk", "c1")["text"] == "travels"
