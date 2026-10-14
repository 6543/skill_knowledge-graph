"""A graph must never reopen silently empty or half-recovered.

Reported twice from cloud sessions: an import succeeded, a later command found
zero records at revision 1 while the RDF index still held every triple. The
state is reproduced by losing SQLite's write-ahead log after a killed process.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from store import KG, GraphStateError

SCRIPTS = Path(__file__).resolve().parents[1]

KILLED_WRITE = """
import os, sys
sys.path.insert(0, sys.argv[1])
from store import KG
graph = KG("g", create=True)
graph.load([{"label": "Lesson", "id": "l1", "props": {"statement": "kept"}}], [])
os._exit(0)
"""


def lose_sidecars(home: Path):
    for suffix in ("-wal", "-shm"):
        sidecar = home / "content" / f"g.sqlite3{suffix}"
        if sidecar.exists():
            sidecar.unlink()


def test_committed_write_survives_killed_process_and_lost_sidecars(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    KG("g", create=True).close()
    env = {**os.environ, "KG_HOME": str(tmp_path)}
    subprocess.run(
        [sys.executable, "-c", KILLED_WRITE, str(SCRIPTS)], check=True, env=env
    )
    lose_sidecars(tmp_path)
    with KG("g") as graph:
        assert graph.props("Lesson", "l1")["statement"] == "kept"


def test_content_older_than_rdf_index_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    KG("g", create=True).close()
    content = tmp_path / "content" / "g.sqlite3"
    shutil.copy(content, tmp_path / "old.sqlite3")
    with KG("g") as graph:
        graph.load([{"label": "Lesson", "id": "l1", "props": {"statement": "x"}}], [])
    shutil.copy(tmp_path / "old.sqlite3", content)
    lose_sidecars(tmp_path)

    with pytest.raises(GraphStateError, match="older than its RDF index"):
        KG("g")
    # Nothing was rebuilt or cleared: a second open still refuses.
    with pytest.raises(GraphStateError):
        KG("g")


def test_missing_content_with_stamped_rdf_is_not_silently_rebuilt(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("g", create=True) as graph:
        graph.load(
            [{"label": "Chunk", "id": "c", "props": {"path": "a", "text": "body"}}], []
        )
    (tmp_path / "content" / "g.sqlite3").unlink()
    lose_sidecars(tmp_path)
    with pytest.raises(GraphStateError):
        KG("g")


def test_empty_content_with_populated_rdf_fails_for_unstamped_graphs(
    tmp_path, monkeypatch
):
    """Graphs written before the stamp existed get the same protection."""
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("g", create=True) as graph:
        graph.load([{"label": "Lesson", "id": "l1", "props": {"statement": "x"}}], [])
        graph._clear_state_stamp()
    db = sqlite3.connect(tmp_path / "content" / "g.sqlite3")
    with db:
        db.execute("DELETE FROM records")
        db.execute(
            "UPDATE metadata SET value='1' WHERE key IN ('revision','rdf_revision')"
        )
    db.close()
    with pytest.raises(GraphStateError):
        KG("g")


def test_stats_and_queries_ignore_the_state_stamp(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("g", create=True) as graph:
        graph.load([{"label": "Lesson", "id": "l1", "props": {}}], [])
        assert graph.stats()["triples"] == 2
        assert graph.q("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }") == [2]


def test_record_keys_are_stored_once(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("g", create=True) as graph:
        sql = graph.content.execute(
            "SELECT sql FROM sqlite_master WHERE name='records'"
        ).fetchone()[0]
        assert "WITHOUT ROWID" in sql
        assert graph.content.execute("PRAGMA page_size").fetchone()[0] == 16384
        indexes = graph.content.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='records'"
        ).fetchall()
        assert indexes == []


def test_old_layout_is_migrated_on_open_and_import(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    with KG("g", create=True) as graph:
        graph.load([{"label": "Lesson", "id": "l1", "props": {"statement": "x"}}], [])
        expected = graph.export_records()
        package = tmp_path / "p.graph-skill-db"
        graph.export_package(package)
    db = sqlite3.connect(tmp_path / "content" / "g.sqlite3")
    db.execute("PRAGMA page_size=4096")
    db.execute("VACUUM")
    with db:
        db.execute("ALTER TABLE records RENAME TO records_new")
        db.execute(
            "CREATE TABLE records(kind TEXT NOT NULL, key TEXT NOT NULL,"
            " value TEXT NOT NULL, PRIMARY KEY(kind,key))"
        )
        db.execute("INSERT INTO records SELECT * FROM records_new")
        db.execute("DROP TABLE records_new")
        db.execute("DELETE FROM metadata WHERE key='schema'")
    db.close()

    with KG("g") as graph:
        assert graph.import_package(package)["created"] == 0
        assert graph.export_records() == expected
        sql = graph.content.execute(
            "SELECT sql FROM sqlite_master WHERE name='records'"
        ).fetchone()[0]
        assert "WITHOUT ROWID" in sql
        assert graph.content.execute("PRAGMA page_size").fetchone()[0] == 16384


def test_large_loads_compact_the_file_and_small_deltas_do_not(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path))
    statements = []
    with KG("g", create=True) as graph:
        graph.content.set_trace_callback(statements.append)
        graph.load(
            [{"label": "Lesson", "id": f"l{i}", "props": {}} for i in range(2000)], []
        )
        assert "VACUUM" in statements
        statements.clear()
        graph.load([{"label": "Lesson", "id": "extra", "props": {}}], [])
        assert "VACUUM" not in statements
