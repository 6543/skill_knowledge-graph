"""Package members must hold exactly the graph's records, however they are built.

Export writes stored rows straight into the package and only rebuilds the
Chunk rows whose text lives in the chunks table. These pin that the result is
byte for byte what serializing `export_records()` gives, for every chunk state.
"""

from __future__ import annotations

import zipfile

from store import KG, canonical


def chunk(name, text, **extra):
    return {
        "label": "Chunk",
        "id": f"repo:{name}#1",
        "props": {"path": name, "source": "repo", "text": text, **extra},
    }


NODES = [
    {"label": "Source", "id": "repo", "props": {"roots": ["repo"]}},
    {"label": "Document", "id": "repo:a.md", "props": {"source": "repo"}},
    chunk("a.md", "plain chunk text"),
    chunk("ü/ß ä.md", "unicode path and text: grüße ✓"),
    # The evicted state: no text, a stub, and no row in the chunks table.
    {
        "label": "Chunk",
        "id": "repo:cold.md#1",
        "props": {"path": "cold.md", "source": "repo", "evicted": "2026-01-01"},
    },
    {
        "label": "Artifact",
        "id": "repo:bin",
        "props": {"path": "bin", "sha256": "0" * 64, "source": "repo"},
    },
    {
        "label": "Lesson",
        "id": "lesson:one",
        "props": {"statement": "keep evidence", "n": 1.5, "tags": ["x", None]},
    },
    {"label": "Thing", "id": "no-props"},
]
EDGES = [
    {
        "rel": "HAS_CHUNK",
        "src_label": "Document",
        "src": "repo:a.md",
        "dst_label": "Chunk",
        "dst": "repo:a.md#1",
    },
    {
        "rel": "SUPPORTED_BY",
        "src_label": "Lesson",
        "src": "lesson:one",
        "dst_label": "Chunk",
        "dst": "repo:a.md#1",
        "props": {"weight": 2, "note": "ok"},
    },
]


def package_lines(path, member):
    with zipfile.ZipFile(path) as archive:
        return archive.read(member).decode("utf-8").splitlines()


def test_package_members_equal_serialized_export_records(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "home"))
    with KG("export", create=True) as kg:
        kg.load(NODES, EDGES)
        # A chunk whose text stayed inline in its record (written before text
        # was stored once, disagreeing with the index) is exported as stored.
        kg.content.execute(
            "UPDATE records SET value=? WHERE kind='node' AND key=?",
            (
                canonical(chunk("a.md", "inline copy wins")),
                canonical(["Chunk", "repo:a.md#1"]),
            ),
        )
        kg.content.commit()
        nodes, edges = kg.export_records()
        texts = {
            n["id"]: n["props"].get("text") for n in nodes if n["label"] == "Chunk"
        }
        assert texts == {
            "repo:a.md#1": "inline copy wins",
            "repo:cold.md#1": None,
            "repo:ü/ß ä.md#1": "unicode path and text: grüße ✓",
        }

        path = kg.export_package(tmp_path / "out")["package"]

    assert package_lines(path, "nodes.jsonl") == [canonical(n) for n in nodes]
    assert package_lines(path, "edges.jsonl") == [canonical(e) for e in edges]
    manifest, read_nodes, read_edges = KG.read_package(path)
    assert (read_nodes, read_edges) == (nodes, edges)
    assert manifest["sources"] == ["repo"]


def test_empty_graph_exports_empty_members(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "home"))
    with KG("empty", create=True) as kg:
        path = kg.export_package(tmp_path / "empty")["package"]
    assert package_lines(path, "nodes.jsonl") == []
    manifest, nodes, edges = KG.read_package(path)
    assert (nodes, edges, manifest["labels"], manifest["sources"]) == ([], [], {}, [])
