from __future__ import annotations

import gc
import json

import pytest
from store import KG, GraphValidationError, validate_graph


def sample_graph():
    nodes = [
        {
            "label": "File",
            "id": "repo:store.go",
            "props": {"path": "store.go", "repository": "repo"},
        },
        {
            "label": "Chunk",
            "id": "repo:store.go#1",
            "props": {
                "path": "store.go",
                "repository": "repo",
                "start_line": 1,
                "end_line": 10,
                "text": "type Store interface { GetUser(id int64) error }",
            },
        },
        {
            "label": "Chunk",
            "id": "repo:store.go#2",
            "props": {
                "path": "store.go",
                "repository": "repo",
                "start_line": 11,
                "end_line": 20,
                "text": "CreatePipeline persists a pipeline and its steps.",
            },
        },
    ]
    edges = [
        {
            "rel": "HAS_CHUNK",
            "src_label": "File",
            "src": "repo:store.go",
            "dst_label": "Chunk",
            "dst": "repo:store.go#1",
            "props": {"source": "fixture", "line": 1},
        },
        {
            "rel": "HAS_CHUNK",
            "src_label": "File",
            "src": "repo:store.go",
            "dst_label": "Chunk",
            "dst": "repo:store.go#2",
        },
    ]
    return nodes, edges


def test_validation_rejects_dangling_edges_and_unsafe_names():
    nodes, edges = sample_graph()
    bad_edge = dict(edges[0], dst="missing")
    with pytest.raises(GraphValidationError, match="missing endpoint"):
        validate_graph(nodes, [bad_edge])

    bad_node = {"label": "Bad Label", "id": "x", "props": {}}
    with pytest.raises(GraphValidationError, match="invalid label"):
        validate_graph([bad_node], [])


def test_load_replaces_stale_facts_and_keeps_chunk_text_out_of_rdf(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    nodes, edges = sample_graph()
    graph = KG("test", create=True)
    graph.load(nodes, edges)

    assert graph.stats()["nodes"] == 3
    assert graph.q("SELECT (COUNT(?text) AS ?count) WHERE { ?x kg:text ?text }") == [0]
    assert graph.edge_props(
        "HAS_CHUNK", "File", "repo:store.go", "Chunk", "repo:store.go#1"
    ) == {
        "line": 1,
        "source": "fixture",
    }

    graph.load([nodes[0]], [], replace=True)
    assert graph.stats()["nodes"] == 1
    assert graph.search_keywords(["GetUser"], scope="file") == []


def test_file_scope_search_joins_evidence_across_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    nodes, edges = sample_graph()
    graph = KG("test", create=True)
    graph.load(nodes, edges)

    results = graph.search_keywords(
        ["type Store interface", "GetUser", "CreatePipeline"], scope="file"
    )
    assert [result["path"] for result in results] == ["store.go"]
    assert {hit["id"] for hit in results[0]["evidence"]} == {
        "repo:store.go#1",
        "repo:store.go#2",
    }
    assert graph.reach("File", "repo:store.go", ("HAS_CHUNK",), "Chunk") == [
        "repo:store.go#1",
        "repo:store.go#2",
    ]
    assert graph.path(
        "File", "repo:store.go", "Chunk", "repo:store.go#2", ("HAS_CHUNK",)
    ) == ["repo:store.go", "repo:store.go#2"]

    graph.content.close()
    del graph
    gc.collect()
    reopened = KG("test")
    assert reopened.search_keywords(["GetUser"], scope="file")[0]["path"] == "store.go"


def test_question_search_returns_ranked_traceable_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    nodes, edges = sample_graph()
    graph = KG("test", create=True)
    graph.load(nodes, edges)

    results = graph.search_question("Where is the server storage interface defined?")
    assert results[0]["repository"] == "repo"
    assert results[0]["path"] == "store.go"
    assert results[0]["evidence"][0]["start_line"] >= 1
    assert len(results[0]["evidence"][0]["text"]) <= 1200


def test_json_and_jsonl_input_are_compatible(tmp_path):
    nodes, _edges = sample_graph()
    array_path = tmp_path / "nodes.json"
    lines_path = tmp_path / "nodes.jsonl"
    array_path.write_text(json.dumps(nodes))
    lines_path.write_text("".join(json.dumps(node) + "\n" for node in nodes))

    from store import read_records

    assert list(read_records(array_path)) == list(read_records(lines_path))


def test_only_source_roots_merge_as_a_set_other_fields_still_conflict(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    source = lambda **props: {"label": "Source", "id": "s", "props": props}
    with KG("roots-scope", create=True) as graph:
        graph.load([source(name="s", roots=["b", "a"])], [])
        graph.load([source(roots=["c", "a"])], [])
        assert graph.props("Source", "s")["roots"] == ["a", "b", "c"]
        with pytest.raises(GraphValidationError, match="conflict"):
            graph.load([source(name="other")], [])
        other = {"label": "Thing", "id": "t", "props": {"roots": ["a"]}}
        graph.load([other], [])
        with pytest.raises(GraphValidationError, match="conflict"):
            graph.load([{**other, "props": {"roots": ["b"]}}], [])
        with pytest.raises(GraphValidationError, match="conflict"):
            graph.load([source(roots="not-a-list")], [])
