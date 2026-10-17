"""Evidence-preserving source refresh, package diff, and memory-only merges."""

from __future__ import annotations

import pytest

import store
from ingest import build_graph
from store import KG, GraphValidationError, diff_records

DAY = 20600


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch):
    monkeypatch.setattr(store, "usage_day", lambda: DAY)


def ingest(root, files):
    root.mkdir(exist_ok=True)
    for old in root.iterdir():
        old.unlink()
    for name, text in files.items():
        (root / name).write_text(text)
    return build_graph([root], "src")


def doc_id(nodes, path):
    return next(
        n["id"]
        for n in nodes
        if n["label"] == "Document" and n["props"]["path"] == path
    )


def chunk_of(nodes, document):
    return next(
        n["id"]
        for n in nodes
        if n["label"] == "Chunk" and n["props"]["document_id"] == document
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "home"))
    raw = tmp_path / "src"
    first = ingest(
        raw,
        {
            "keep.md": "# Keep\nunchanged\n",
            "cited.md": "# Cited\nold fact\n",
            "gone.md": "# Gone\nnobody cites this\n",
        },
    )
    graph = KG("g", create=True)
    graph.load(*first)
    cited_doc = doc_id(first[0], "cited.md")
    graph.load(
        [{"label": "Lesson", "id": "l1", "props": {"statement": "fact"}}],
        [
            {
                "rel": "SUPPORTED_BY",
                "src_label": "Lesson",
                "src": "l1",
                "dst_label": "Chunk",
                "dst": chunk_of(first[0], cited_doc),
            }
        ],
    )
    yield graph, raw, first
    graph.close()


def test_refresh_marks_cited_stale_versions_and_deletes_uncited_ones(setup):
    graph, raw, first = setup
    fresh = ingest(
        raw,
        {
            "keep.md": "# Keep\nunchanged\n",
            "cited.md": "# Cited\nnew fact\n",
            "added.md": "# Added\nbrand new\n",
        },
    )
    preview = graph.refresh_source("src", *fresh)
    assert preview["dry_run"] is True
    assert graph.exists("Document", doc_id(first[0], "gone.md"))

    report = graph.refresh_source("src", *fresh, apply=True, superseded_by="rev-2")
    assert report["stale"] == 2 and report["deleted_documents"] == 1
    assert report["marked_outdated"] == 1 and report["added"] == 2
    assert report["evidence_edges_before"] == report["evidence_edges_after"] == 1

    old_cited = doc_id(first[0], "cited.md")
    props = graph.props("Document", old_cited)
    assert props["outdated"] is True and props["outdated_since"] == store.day_iso(DAY)
    assert props["superseded_by"] == "rev-2"
    assert graph.props("Chunk", chunk_of(first[0], old_cited))["outdated"] is True
    assert not graph.exists("Document", doc_id(first[0], "gone.md"))
    assert graph.exists("Document", doc_id(first[0], "keep.md"))
    assert not graph.props("Document", doc_id(first[0], "keep.md")).get("outdated")

    again = graph.refresh_source("src", *fresh)
    assert again["stale"] == 0 and again["added"] == 0


def test_a_version_that_comes_back_is_no_longer_outdated(setup):
    graph, raw, first = setup
    graph.refresh_source(
        "src", *ingest(raw, {"keep.md": "# Keep\nunchanged\n"}), apply=True
    )
    old_cited = doc_id(first[0], "cited.md")
    assert graph.props("Document", old_cited)["outdated"] is True
    report = graph.refresh_source("src", *first, apply=True)
    assert report["revived"] == 1
    assert "outdated" not in graph.props("Document", old_cited)


def test_a_later_refresh_keeps_the_first_outdated_date(setup, monkeypatch):
    graph, raw, first = setup
    only_keep = ingest(raw, {"keep.md": "# Keep\nunchanged\n"})
    graph.refresh_source("src", *only_keep, apply=True)
    old_cited = doc_id(first[0], "cited.md")
    monkeypatch.setattr(store, "usage_day", lambda: DAY + 7)
    graph.refresh_source("src", *only_keep, apply=True)
    assert graph.props("Document", old_cited)["outdated_since"] == store.day_iso(DAY)


def test_refresh_rejects_records_of_another_source(setup, tmp_path):
    graph, _raw, _first = setup
    other = ingest(tmp_path / "other", {"x.md": "x"})
    nodes = [dict(n, props={**n["props"], "source": "elsewhere"}) for n in other[0]]
    with pytest.raises(GraphValidationError, match="another source"):
        graph.refresh_source("src", nodes, [])


def test_bookkeeping_relations_do_not_count_as_evidence(setup, tmp_path):
    graph, raw, first = setup
    gone = doc_id(first[0], "gone.md")
    graph.load(
        [{"label": "Snapshot", "id": "snap", "props": {}}],
        [
            {
                "rel": "INCLUDES",
                "src_label": "Snapshot",
                "src": "snap",
                "dst_label": "Document",
                "dst": gone,
            }
        ],
    )
    fresh = ingest(
        raw, {"keep.md": "# Keep\nunchanged\n", "cited.md": "# Cited\nold fact\n"}
    )
    report = graph.refresh_source(
        "src", *fresh, apply=True, bookkeeping_rels=("INCLUDES",)
    )
    assert report["deleted_documents"] == 1
    assert not graph.exists("Document", gone)


def test_diff_reports_changes_memory_and_duplicate_current_versions(setup, tmp_path):
    graph, raw, _first = setup
    a = graph.export_records()
    fresh = ingest(
        raw,
        {
            "keep.md": "# Keep\nunchanged\n",
            "cited.md": "# Cited\nnew fact\n",
            "gone.md": "# Gone\nnobody cites this\n",
        },
    )
    graph.load(*fresh)
    graph.load(
        [{"label": "Pitfall", "id": "p1", "props": {"statement": "only in b"}}], []
    )
    b = graph.export_records()

    report = diff_records(*a, *b)
    assert report["nodes"]["only_b"] == {"Chunk": 1, "Document": 1, "Pitfall": 1}
    assert report["nodes"]["only_a"] == {}
    assert [m["id"] for m in report["memory"]["only_b"]] == ["p1"]
    assert report["current_versions"]["paths"] == 1
    assert report["current_versions"]["examples"][0]["path"] == "cited.md"


def test_memory_only_import_brings_notes_and_their_evidence_not_sources(
    setup, tmp_path
):
    graph, _raw, first = setup
    package = tmp_path / "fork.graph-skill-db"
    graph.load([{"label": "Decision", "id": "d1", "props": {"statement": "use x"}}], [])
    graph.export_package(package)

    with KG("base", create=True) as base:
        base.load(*ingest(tmp_path / "base-src", {"other.md": "# Other\ntext\n"}))
        report = base.import_package(package, memory_only=True)
        assert base.exists("Decision", "d1") and base.exists("Lesson", "l1")
        # The cited chunk comes along as evidence; uncited source files do not.
        assert base.exists("Chunk", chunk_of(first[0], doc_id(first[0], "cited.md")))
        assert not base.exists("Document", doc_id(first[0], "gone.md"))
    assert report["memory_only"] == {"memory_nodes": 2, "evidence_nodes": 1, "edges": 1}


def test_rechunked_unchanged_documents_drop_their_old_chunks(setup, tmp_path):
    graph, raw, _first = setup
    long_text = (
        "# Keep\n" + "\n".join(f"line {i} " + "x" * 40 for i in range(40)) + "\n"
    )
    (raw / "keep.md").write_text(long_text)
    before = build_graph([raw], "src", max_chars=10_000)
    graph.refresh_source("src", *before, apply=True)
    keep = doc_id(before[0], "keep.md")
    old_chunks = {
        n["id"]
        for n in before[0]
        if n["label"] == "Chunk" and n["props"]["document_id"] == keep
    }

    rechunked = build_graph([raw], "src", max_chars=500)
    assert doc_id(rechunked[0], "keep.md") == keep
    report = graph.refresh_source("src", *rechunked, apply=True)
    assert report["stale_chunks"] == len(old_chunks)
    stored = {
        n["id"]
        for n in graph.export_records()[0]
        if n["label"] == "Chunk" and n["props"].get("document_id") == keep
    }
    assert stored.isdisjoint(old_chunks)


def test_refresh_takes_the_fresh_ingester_fields_of_its_own_records(setup):
    """The same chunk text (same ID) can get new locators from a newer ingester."""
    graph, _raw, first = setup
    keep = doc_id(first[0], "keep.md")
    chunk = chunk_of(first[0], keep)
    nodes = [
        dict(n, props={**n["props"], "end_line": n["props"]["end_line"] + 1})
        if n["id"] == chunk
        else n
        for n in first[0]
    ]
    graph.load(nodes, [], on_conflict="incoming")
    report = graph.refresh_source("src", *first, apply=True)
    assert "conflicts" not in report and report["updated"] >= 1
    assert graph.props("Chunk", chunk)["end_line"] == next(
        n["props"]["end_line"] for n in first[0] if n["id"] == chunk
    )
