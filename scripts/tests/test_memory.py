"""Session memory: knowledge nodes must be findable, listable and easy to add."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from store import KG, GraphValidationError, note_records

SCRIPT = str(Path(__file__).parents[1] / "kg.py")


def chunk(cid, text, source="notes"):
    return {
        "label": "Chunk",
        "id": cid,
        "props": {"path": "n.md", "source": source, "repository": source, "text": text},
    }


LESSON = {
    "label": "Lesson",
    "id": "lesson:pin-digests",
    "props": {
        "statement": "Pin plugin images by digest in the root CI config",
        "observed": "2026-09-20",
        "source": "session-2026-09-20",
    },
}
CONVENTION = {
    "label": "Convention",
    "id": "convention:shell",
    "props": {"statement": "Shell is fish on NixOS", "source": "session-2026-09-24"},
}
URL = {"label": "WebResource", "id": "https://digest.example", "props": {}}


@pytest.fixture
def kg(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with KG("memory", create=True) as graph:
        graph.load(
            [LESSON, CONVENTION, URL, chunk("c1", "unrelated chunk about digest")],
            [],
        )
        yield graph


def test_find_matches_knowledge_node_properties(kg):
    hits = kg.find("how do we pin images?")
    assert [hit["id"] for hit in hits] == ["lesson:pin-digests"]
    assert hits[0]["props"]["statement"].startswith("Pin plugin images")


def test_find_skips_ingest_labels_unless_asked(kg):
    assert all(hit["label"] != "WebResource" for hit in kg.find("digest"))
    assert [hit["id"] for hit in kg.find("digest", labels=["WebResource"])] == [
        "https://digest.example"
    ]


def test_search_returns_matching_knowledge_nodes_beside_chunks(kg):
    result = kg.search(["pin", "digest"])
    assert result["nodes"][0]["id"] == "lesson:pin-digests"
    assert result["results"], "chunk evidence still comes back"


def test_node_index_follows_updates_and_removal(kg):
    kg.load(
        [{**LESSON, "props": {"statement": "Use tag at digest pins"}}],
        [],
        on_conflict="incoming",
    )
    assert kg.find("plugin images") == []
    assert [hit["id"] for hit in kg.find("tag pins")] == ["lesson:pin-digests"]
    kg.drop_source("session-2026-09-20")
    assert kg.find("tag pins") == []


def test_node_index_is_built_for_graphs_written_before_it_existed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with KG("old", create=True) as graph:
        graph.load([LESSON], [])
        graph.content.execute("DROP TABLE node_index")
        graph.content.commit()
    with KG("old") as graph:
        assert [hit["id"] for hit in graph.find("plugin images")] == [LESSON["id"]]


def test_brief_lists_memory_newest_first_and_counts_the_rest(kg):
    kg.load(
        [
            {
                "label": "Lesson",
                "id": "lesson:newer",
                "props": {"statement": "Newer lesson", "observed": "2026-09-29"},
            }
        ],
        [],
    )
    brief = kg.brief()
    lessons = brief["memory"]["Lesson"]
    assert [item["id"] for item in lessons["items"]] == [
        "lesson:newer",
        "lesson:pin-digests",
    ]
    assert lessons["items"][1]["summary"].startswith("Pin plugin images")
    assert brief["memory"]["Convention"]["count"] == 1
    assert brief["other_labels"] == {"Chunk": 1, "WebResource": 1}


def test_note_records_build_a_dated_lesson_with_evidence_links():
    nodes, edges = note_records(
        "Lesson",
        "Always verify patches in a detached worktree",
        supported_by=["Chunk:c1"],
        about=["Convention:convention:shell"],
        props={"severity": "high"},
        today="2026-09-30",
    )
    assert nodes == [
        {
            "label": "Lesson",
            "id": "lesson:always-verify-patches-in-a-detached-worktree",
            "props": {
                "statement": "Always verify patches in a detached worktree",
                "observed": "2026-09-30",
                "source": "session-2026-09-30",
                "severity": "high",
            },
        }
    ]
    assert {(e["rel"], e["dst_label"], e["dst"]) for e in edges} == {
        ("SUPPORTED_BY", "Chunk", "c1"),
        ("ABOUT", "Convention", "convention:shell"),
    }


def test_note_rejects_a_reference_without_a_label():
    with pytest.raises(GraphValidationError, match="LABEL:ID"):
        note_records("Lesson", "x", supported_by=["c1"], today="2026-09-30")


def test_delete_removes_nodes_incident_edges_and_index(kg):
    kg.load(
        [],
        [
            {
                "rel": "SUPPORTED_BY",
                "src_label": "Lesson",
                "src": LESSON["id"],
                "dst_label": "Chunk",
                "dst": "c1",
            }
        ],
    )
    preview = kg.delete(nodes=[("Chunk", "c1")], dry_run=True)
    assert preview == {
        "dry_run": True,
        "nodes_removed": 1,
        "edges_removed": 1,
        "missing": [],
    }
    assert kg.exists("Chunk", "c1")
    kg.delete(nodes=[("Chunk", "c1"), ("Thing", "nope")])
    assert not kg.exists("Chunk", "c1")
    assert kg.neighbors("Lesson", LESSON["id"]) == []
    assert kg.search(["unrelated"])["results"] == []


def test_orphans_are_nodes_of_the_given_labels_without_edges(kg):
    assert kg.orphans(["WebResource"]) == [("WebResource", "https://digest.example")]


def test_dry_run_load_reports_changes_and_conflicts_without_writing(kg):
    report = kg.load(
        [
            {**LESSON, "props": {"statement": "changed"}},
            {"label": "Lesson", "id": "lesson:new", "props": {}},
        ],
        [],
        dry_run=True,
    )
    assert report["dry_run"] is True
    assert report["created"] == 1 and report["updated"] == 0
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["field"] == "statement"
    assert not kg.exists("Lesson", "lesson:new")


def run(*args, check=True):
    result = subprocess.run(
        [sys.executable, SCRIPT, *args], capture_output=True, text=True, check=False
    )
    if check:
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    return result


def test_cli_note_then_find_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    run("note", "cli", "--text", "Fish needs set -x for exports", "--label", "Lesson")
    hits = run("find", "cli", "--text", "fish exports")
    assert hits[0]["label"] == "Lesson"


def test_cli_prune_drops_stale_ingester_mentions_after_merging_old_package(
    tmp_path, monkeypatch
):
    """An old package brings back URL mentions the fixed ingester no longer makes."""
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    text = "see `http://localhost:8000` now"
    cid = "notes:a.md@x#chunk-000000-y"
    nodes = [
        {
            "label": "Chunk",
            "id": cid,
            "props": {**chunk(cid, text)["props"], "extractor": "plain-text@2"},
        },
        {"label": "WebResource", "id": "http://localhost:8000", "props": {}},
        {"label": "WebResource", "id": "http://localhost:8000`", "props": {}},
    ]
    edges = [
        {
            "rel": "LINKS_TO",
            "src_label": "Chunk",
            "src": cid,
            "dst_label": "WebResource",
            "dst": url,
        }
        for url in ("http://localhost:8000", "http://localhost:8000`")
    ]
    (tmp_path / "n.jsonl").write_text("".join(json.dumps(n) + "\n" for n in nodes))
    (tmp_path / "e.jsonl").write_text("".join(json.dumps(e) + "\n" for e in edges))
    run(
        "load",
        "p",
        "--nodes",
        str(tmp_path / "n.jsonl"),
        "--edges",
        str(tmp_path / "e.jsonl"),
    )
    preview = run("prune", "p")
    assert preview["dry_run"] is True
    assert preview["stale_mentions"] == 1 and preview["orphans"] == 1
    run("prune", "p", "--apply")
    with KG("p") as graph:
        assert graph.exists("WebResource", "http://localhost:8000")
        assert not graph.exists("WebResource", "http://localhost:8000`")


def test_cli_show_unknown_node_fails_with_suggestions(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    run("note", "cli", "--text", "Pin images", "--id", "lesson:pin-images")
    result = run("show", "cli", "--label", "Lesson", "--id", "lesson:pin", check=False)
    assert result.returncode == 2
    assert "lesson:pin-images" in result.stderr


def test_brief_dates_undated_notes_by_their_session_source(kg):
    item = kg.brief()["memory"]["Convention"]["items"][0]
    assert item["date"] == "2026-09-24"


def test_cli_renoting_the_same_statement_later_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    run("note", "cli", "--text", "Pin images", "--source", "session-2026-09-01")
    again = run("note", "cli", "--text", "Pin images")
    assert again["created"] == 0 and again["updated"] == 0


def test_cli_note_with_unknown_link_suggests_the_id(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    run("note", "cli", "--label", "Skill", "--text", "gpw", "--id", "skill:gpw")
    result = run("note", "cli", "--text", "x", "--about", "Skill:gpw", check=False)
    assert result.returncode == 2
    assert "Skill:skill:gpw" in result.stderr


def test_cli_compact_search_keeps_the_document_id_of_each_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    nodes = tmp_path / "nodes.jsonl"
    record = chunk("c1", "The server timeout defaults to 30 seconds.")
    record["props"]["document_id"] = "notes:notes.md@abc"
    nodes.write_text(json.dumps(record) + "\n")
    run("load", "cli", "--nodes", str(nodes))
    result = run("search", "cli", "--keyword", "timeout", "--compact")["results"][0]
    assert result["evidence"][0]["document_id"] == "notes:notes.md@abc"
    assert result["document_id"] == "notes:notes.md@abc"


def test_cli_show_and_find_agree_that_props_hold_stored_properties_only(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    run("note", "cli", "--text", "Pin images", "--id", "lesson:pin-images")
    shown = run("show", "cli", "--label", "Lesson", "--id", "lesson:pin-images")
    found = run("find", "cli", "--text", "pin images")[0]
    assert shown["id"] == found["id"] == "lesson:pin-images"
    assert "id" not in shown["props"]
    assert shown["props"] == found["props"]
