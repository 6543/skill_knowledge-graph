"""A large load rebuilds the full-text indexes once instead of row by row.

These pin the contract of that shortcut: the indexes end up exactly as the
per-row triggers would have left them, and the triggers are back afterwards,
also when the load is rolled back.
"""

from __future__ import annotations

import pytest

from store import BULK_INDEX_MIN, FTS_TRIGGER_NAMES, KG, GraphValidationError


def chunk(i, text):
    return {
        "label": "Chunk",
        "id": f"repo:doc{i}.md#1",
        "props": {"path": f"docs/doc{i}.md", "source": "repo", "text": text},
    }


def lesson(text):
    return {"label": "Lesson", "id": f"lesson:{text}", "props": {"statement": text}}


def big_batch():
    nodes = [chunk(i, f"filler text number {i}") for i in range(BULK_INDEX_MIN)]
    nodes[7] = chunk(7, "the zanzibar timeout lives here")
    nodes.append(lesson("prefer quokka retries"))
    return nodes


def triggers(kg):
    return {
        row[0]
        for row in kg.content.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }


def assert_indexes_consistent(kg):
    for table in ("chunks_fts", "node_fts"):
        kg.content.execute(
            f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)"
        )
    locations = kg.content.execute(
        "SELECT rowid, loc FROM locations_fts ORDER BY rowid"
    )
    assert (
        locations.fetchall()
        == kg.content.execute("SELECT rowid, loc FROM chunks ORDER BY rowid").fetchall()
    )


@pytest.fixture
def kg(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("bulk", create=True) as graph:
        yield graph


def test_bulk_load_indexes_everything_and_restores_triggers(kg):
    kg.load(big_batch(), [])

    assert triggers(kg) == set(FTS_TRIGGER_NAMES)
    assert_indexes_consistent(kg)
    assert kg.search_keywords(["zanzibar"])[0]["path"] == "docs/doc7.md"
    assert kg.search(["doc7"], scope="chunk")["results"]
    assert [hit["id"] for hit in kg.find("quokka")] == ["lesson:prefer quokka retries"]

    # Small writes afterwards go through the restored triggers again.
    kg.load([chunk(7, "moved to the yokohama setting")], [], on_conflict="incoming")
    kg.load([lesson("avoid wombat locks")], [])
    kg.delete(nodes=[("Lesson", "lesson:prefer quokka retries")])

    assert_indexes_consistent(kg)
    assert not kg.search_keywords(["zanzibar"])
    assert kg.search_keywords(["yokohama"])[0]["path"] == "docs/doc7.md"
    assert kg.find("quokka") == []
    assert [hit["id"] for hit in kg.find("wombat")] == ["lesson:avoid wombat locks"]


def test_rolled_back_bulk_load_keeps_triggers_and_indexes(kg):
    kg.load([chunk(1, "original wording")], [])
    batch = big_batch()
    batch[1] = chunk(1, "conflicting wording")

    preview = kg.load(batch, [], dry_run=True)
    assert preview["conflicts"]
    with pytest.raises(GraphValidationError):
        kg.load(batch, [])

    assert triggers(kg) == set(FTS_TRIGGER_NAMES)
    assert_indexes_consistent(kg)
    assert not kg.search_keywords(["zanzibar"])
    assert kg.search_keywords(["original"])[0]["path"] == "docs/doc1.md"
