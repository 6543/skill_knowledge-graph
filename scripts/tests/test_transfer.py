"""Merging, source-scoped refresh, and the portable `.graph-skill-db` package."""

from __future__ import annotations

import zipfile

import pytest
from store import KG, PACKAGE_SUFFIX, GraphValidationError


def corpus(source: str, marker: str):
    nodes = [
        {"label": "Source", "id": source, "props": {"name": source}},
        {
            "label": "Document",
            "id": f"{source}:notes.md",
            "props": {"path": "notes.md", "source": source, "repository": source},
        },
        {
            "label": "Chunk",
            "id": f"{source}:notes.md#1",
            "props": {
                "path": "notes.md",
                "source": source,
                "repository": source,
                "start_line": 1,
                "end_line": 2,
                "text": f"{marker} lives in this document.",
            },
        },
    ]
    edges = [
        {
            "rel": "CONTAINS",
            "src_label": "Source",
            "src": source,
            "dst_label": "Document",
            "dst": f"{source}:notes.md",
        },
        {
            "rel": "HAS_CHUNK",
            "src_label": "Document",
            "src": f"{source}:notes.md",
            "dst_label": "Chunk",
            "dst": f"{source}:notes.md#1",
            "props": {"extractor": "fixture"},
        },
    ]
    return nodes, edges


def graph(tmp_path, monkeypatch, name="test"):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    return KG(name, create=True)


def test_merge_adds_a_source_without_discarding_the_first(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch)
    kg.load(*corpus("alpha", "Neutrino"))
    kg.load(*corpus("beta", "Perfectoid"), replace=False)

    assert sorted(kg.stats()["sources"]) == ["alpha", "beta"]
    assert kg.search_keywords(["Neutrino"])
    assert kg.search_keywords(["Perfectoid"])


def test_merge_accepts_edges_pointing_at_nodes_already_in_the_store(
    tmp_path, monkeypatch
):
    """A working session adds facts to what an earlier batch loaded."""
    kg = graph(tmp_path, monkeypatch)
    kg.load(*corpus("alpha", "Neutrino"))

    finding = [{"label": "Finding", "id": "f1", "props": {"confidence": 0.8}}]
    link = [
        {
            "rel": "SUPPORTED_BY",
            "src_label": "Finding",
            "src": "f1",
            "dst_label": "Chunk",
            "dst": "alpha:notes.md#1",
            "props": {"analyst": "session"},
        }
    ]
    result = kg.load(finding, link, replace=False)

    assert result["mode"] == "merge"
    assert result["endpoints_resolved_in_store"] == 1
    assert kg.neighbors("Finding", "f1") == [
        {"rel": "SUPPORTED_BY", "label": "Chunk", "id": "alpha:notes.md#1"}
    ]
    assert kg.edge_props(
        "SUPPORTED_BY", "Finding", "f1", "Chunk", "alpha:notes.md#1"
    ) == {"analyst": "session"}


def test_merge_upserts_properties_instead_of_stacking_them(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch)
    kg.load([{"label": "Finding", "id": "f1", "props": {"status": "draft"}}], [])
    kg.load(
        [{"label": "Finding", "id": "f1", "props": {"status": "confirmed"}}],
        [],
        replace=False,
        on_conflict="incoming",
    )

    assert kg.props("Finding", "f1")["status"] == "confirmed"
    assert kg.q("SELECT ?s WHERE { ?x kg:status ?s }") == ["confirmed"]


def test_source_scoped_replace_refreshes_one_corpus_only(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch)
    kg.load(*corpus("alpha", "Neutrino"))
    kg.load(*corpus("beta", "Perfectoid"), replace=False)

    refreshed = corpus("beta", "Condensed")
    result = kg.load(*refreshed, source="beta")

    assert result["mode"] == "replace_source"
    assert kg.search_keywords(["Neutrino"]), "the untouched source must survive"
    assert kg.search_keywords(["Condensed"]), "the refreshed source must be searchable"
    assert kg.search_keywords(["Perfectoid"]) == [], "stale text must not linger"


def test_dropping_a_source_leaves_no_orphan_documents(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch)
    kg.load(*corpus("alpha", "Neutrino"))
    kg.load(*corpus("beta", "Perfectoid"), replace=False)

    kg.drop_source("beta")

    assert kg.stats()["sources"] == ["alpha"]
    assert kg.q("SELECT ?id WHERE { ?d a kg:Document ; kg:id ?id }") == [
        "alpha:notes.md"
    ]


def test_package_round_trip_preserves_nodes_edges_text_and_provenance(
    tmp_path, monkeypatch
):
    kg = graph(tmp_path, monkeypatch, "origin")
    kg.load(*corpus("alpha", "Neutrino"))
    before_nodes, before_edges = kg.export_records()

    package = kg.export_package(tmp_path / "share")
    assert package["package"].endswith(PACKAGE_SUFFIX)
    with zipfile.ZipFile(package["package"]) as archive:
        assert sorted(archive.namelist()) == [
            "edges.jsonl",
            "manifest.json",
            "nodes.jsonl",
        ]

    restored = KG("restored", create=True)
    restored.import_package(package["package"])
    after_nodes, after_edges = restored.export_records()

    assert after_nodes == before_nodes
    assert after_edges == before_edges
    assert restored.props("Chunk", "alpha:notes.md#1")["text"].startswith("Neutrino")
    assert restored.edge_props(
        "HAS_CHUNK", "Document", "alpha:notes.md", "Chunk", "alpha:notes.md#1"
    ) == {"extractor": "fixture"}


def test_import_merges_into_an_existing_graph_by_default(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch, "origin")
    kg.load(*corpus("alpha", "Neutrino"))
    package = kg.export_package(tmp_path / "alpha")["package"]

    target = KG("target", create=True)
    target.load(*corpus("beta", "Perfectoid"))
    result = target.import_package(package)

    assert result["mode"] == "merge"
    assert sorted(target.stats()["sources"]) == ["alpha", "beta"]
    assert target.search_keywords(["Perfectoid"]), (
        "the existing graph must not be replaced"
    )


def test_import_rejects_archives_that_are_not_graph_packages(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch)
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as archive:
        archive.writestr("readme.txt", "not a graph")

    with pytest.raises(GraphValidationError, match="missing"):
        kg.import_package(plain)

    loose = tmp_path / "notes.txt"
    loose.write_text("just text")
    with pytest.raises(GraphValidationError, match="not a zip"):
        kg.read_package(loose)


def test_dropping_a_source_takes_its_edge_provenance_with_it(tmp_path, monkeypatch):
    """A reified edge fact belongs to no source, so it has to be purged explicitly
    or its predicate and properties outlive the endpoints they described."""
    kg = graph(tmp_path, monkeypatch)
    kg.load(*corpus("alpha", "Neutrino"))

    report = kg.drop_source("alpha")

    assert report["edge_facts_removed"] == 1
    assert len(kg.store) == 0


def test_a_source_with_no_chunks_is_still_reported(tmp_path, monkeypatch):
    kg = graph(tmp_path, monkeypatch)
    kg.load(
        [
            {"label": "Source", "id": "images", "props": {"name": "images"}},
            {
                "label": "Artifact",
                "id": "images:diagram.png",
                "props": {"source": "images", "path": "diagram.png"},
            },
        ],
        [
            {
                "rel": "CONTAINS",
                "src_label": "Source",
                "src": "images",
                "dst_label": "Artifact",
                "dst": "images:diagram.png",
            }
        ],
    )

    assert kg.stats()["sources"] == ["images"]
