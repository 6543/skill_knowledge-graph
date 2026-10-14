from __future__ import annotations

from pathlib import Path

import pytest
from ingest import build_graph, chunk_text


def test_markdown_chunks_keep_headings_and_line_ranges():
    chunks = chunk_text("# One\nalpha\n\n## Two\nbeta\n", ".md", max_chars=100)
    assert [(chunk.heading, chunk.start_line, chunk.end_line) for chunk in chunks] == [
        ("One", 1, 3),
        ("Two", 4, 5),
    ]


def test_ingest_is_deterministic_and_records_binary_artifacts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "readme.md").write_text("# Hello\nWOODPECKER_HOST is configured here.\n")
    (source / "asset.bin").write_bytes(b"\x00\x01\x02")

    first = build_graph([source], source_name="fixture")
    second = build_graph([source], source_name="fixture")
    assert first == second

    nodes, edges = first
    labels = {node["label"] for node in nodes}
    assert {"Source", "Document", "Chunk", "Artifact", "EnvironmentVariable"} <= labels
    assert any(edge["rel"] == "MENTIONS_ENV" for edge in edges)


def test_ingest_strips_html_without_a_file_extension(tmp_path):
    page = tmp_path / "body"
    page.write_text(
        "<!doctype html><html><style>.noise{color:red}</style><body><p>Hello graph</p></body></html>"
    )
    nodes, _edges = build_graph([page], source_name="fixture")
    text = next(node["props"]["text"] for node in nodes if node["label"] == "Chunk")
    assert text == "Hello graph"


def test_oversized_files_become_artifacts_without_being_read_into_memory(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "huge.txt").write_text("x" * 5000)
    (source / "small.md").write_text("# Fine\nshort enough\n")

    nodes, _edges = build_graph([source], source_name="fixture", max_file_bytes=1000)

    artifact = next(node for node in nodes if node["label"] == "Artifact")
    assert artifact["props"]["path"] == "huge.txt"
    assert artifact["props"]["size_bytes"] == 5000
    assert len(artifact["props"]["sha256"]) == 64
    assert any(node["label"] == "Chunk" for node in nodes)


def test_source_roots_do_not_bake_in_an_absolute_machine_path(tmp_path, monkeypatch):
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "a.md").write_text("# A\ntext\n")
    monkeypatch.chdir(tmp_path)

    nodes, _edges = build_graph([Path("corpus")], source_name="fixture")

    roots = next(node for node in nodes if node["label"] == "Source")["props"]["roots"]
    assert roots == ["corpus"]


def test_pdf_and_svg_remain_artifacts(tmp_path):
    (tmp_path / "paper.pdf").write_text("%PDF-1.7\nASCII object text")
    (tmp_path / "diagram.svg").write_text("<svg><text>label</text></svg>")
    nodes, _ = build_graph([tmp_path], "media")
    assert len([n for n in nodes if n["label"] == "Artifact"]) == 2
    assert not any(n["label"] == "Chunk" for n in nodes)


def test_invalid_limits_and_missing_input_fail(tmp_path):
    with pytest.raises(ValueError):
        chunk_text("x", ".txt", max_chars=0)
    with pytest.raises(FileNotFoundError):
        build_graph([tmp_path / "missing"], "missing")


def test_changed_documents_are_versioned_and_mergeable(tmp_path, monkeypatch):
    from store import KG

    source = tmp_path / "source"
    source.mkdir()
    file = source / "notes.md"
    file.write_text("old content")
    first = build_graph([source], "docs")
    file.write_text("new content")
    second = build_graph([source], "docs")
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with KG("versions", create=True) as graph:
        graph.load(*first)
        graph.load(*second)
        assert graph.stats()["labels"]["Document"] == 2
        assert graph.search_keywords(["old content"])
        assert graph.search_keywords(["new content"])


def test_html_locators_describe_derived_text(tmp_path):
    page = tmp_path / "body"
    page.write_text("<html><body><p>First</p><p>Second</p></body></html>")
    nodes, _ = build_graph([page], "web")
    chunk = next(n for n in nodes if n["label"] == "Chunk")
    assert chunk["props"]["locator_basis"] == "derived_text_lines"
    assert chunk["props"]["extractor"] == "html-text@2"


def test_virtualenvs_and_vendored_trees_are_not_ingested(tmp_path):
    """An exact-match skip list misses `.venv-kg`, `venv`, and `.direnv`, and one
    missed virtualenv buries the corpus under thousands of vendored files."""
    source = tmp_path / "project"
    (source / ".venv-kg" / "lib" / "site-packages").mkdir(parents=True)
    (source / "venv" / "bin").mkdir(parents=True)
    (source / "node_modules" / "left-pad").mkdir(parents=True)
    source.mkdir(exist_ok=True)
    (source / "README.md").write_text("# Real\nthe corpus\n")
    (source / ".venv-kg" / "lib" / "site-packages" / "vendored.py").write_text("x = 1\n")
    (source / "venv" / "bin" / "activate").write_text("export PATH=x\n")
    (source / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")

    nodes, _edges = build_graph([source], source_name="fixture")

    paths = {node["props"]["path"] for node in nodes if node["label"] == "Document"}
    assert paths == {"README.md"}
