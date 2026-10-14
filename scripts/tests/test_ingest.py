from __future__ import annotations

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
