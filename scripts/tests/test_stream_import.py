"""Package size limit and the streamed, staged import of large packages."""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import zipfile

import pytest

import kg as cli
import store
from store import (
    KG,
    GraphValidationError,
    PackageTooLargeError,
    usage_key,
)

BATCH = 3  # tiny batches, so a handful of records already streams


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    monkeypatch.setenv("KG_HOME", str(path))
    monkeypatch.setenv("KG_USAGE", "0")
    return path


def thing(identifier, **props):
    return {"label": "Thing", "id": identifier, "props": props}


def link(src, dst, rel="NEXT", **props):
    edge = {
        "rel": rel,
        "src_label": "Thing",
        "src": src,
        "dst_label": "Thing",
        "dst": dst,
    }
    if props:
        edge["props"] = props
    return edge


def corpus(count=10, marker="v1"):
    nodes = [thing(f"t{i}", n=i, marker=marker) for i in range(count)]
    nodes += [
        {"label": "Source", "id": "src", "props": {"name": "src"}},
        {
            "label": "Chunk",
            "id": "src:doc#1",
            "props": {"source": "src", "path": "doc.md", "text": "neutrino line"},
        },
        {"label": "Lesson", "id": "l1", "props": {"statement": "keep batches small"}},
    ]
    edges = [link(f"t{i}", f"t{i + 1}", weight=i) for i in range(count - 1)]
    edges.append(
        {
            "rel": "ABOUT",
            "src_label": "Lesson",
            "src": "l1",
            "dst_label": "Thing",
            "dst": "t0",
        }
    )
    return nodes, edges


def package_of(name, records, tmp_path, use=()):
    with KG(name, create=True) as graph:
        graph.load(*records)
        if use:
            graph.track_usage = True
            graph.record_use(hits=use)
        return graph.export_package(tmp_path / f"{name}.graph-skill-db")["package"]


def snapshot(graph):
    nodes, edges = graph.export_records()
    rows = graph.q("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")
    return {
        "nodes": sorted(map(json.dumps, nodes)),
        "edges": sorted(map(json.dumps, edges)),
        "triples": graph._triple_count(),
        "sparql": rows,
        "chunk_hits": json.dumps(graph.search(["neutrino"])["results"], sort_keys=True),
        "node_hits": [hit["id"] for hit in graph.find(["batches"])],
    }


def leftovers(home):
    return sorted(
        p.name
        for p in home.rglob("*")
        if any(mark in p.name for mark in (".import-", ".old-", ".failed-"))
    )


# --------------------------------------------------------------------- limit


def test_oversized_package_names_the_doubled_limit(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    for read in (
        lambda: KG.read_package(package, max_bytes=100),
        lambda: KG("dst", create=True).import_package(package, max_bytes=100),
        lambda: KG("dst2", create=True).import_package(
            package, max_bytes=100, batch_records=BATCH
        ),
    ):
        with pytest.raises(PackageTooLargeError) as caught:
            read()
        assert caught.value.limit == 100
        assert caught.value.next_limit == 200
        assert caught.value.size > 100
        assert "--max-bytes 200" in str(caught.value)
        assert "max_bytes" in str(caught.value)  # the marker callers already match


def test_export_over_the_limit_names_the_doubled_limit(home, tmp_path):
    with KG("src", create=True) as graph:
        graph.load(*corpus())
        with pytest.raises(PackageTooLargeError) as caught:
            graph.export_package(tmp_path / "p", max_bytes=64)
    assert caught.value.next_limit == 128
    assert "--max-bytes 128" in str(caught.value)


def test_cli_reports_the_doubled_limit_and_a_retry_with_it_succeeds(
    home, tmp_path, capsys
):
    package = package_of("src", corpus(), tmp_path)
    tried = [100]
    while True:  # what the skill tells Claude to do, one user approval per step
        args = cli.build_parser().parse_args(
            [
                "import",
                "dst",
                "--file",
                package,
                "--max-bytes",
                str(tried[-1]),
            ]
        )
        try:
            with KG("dst", create=True) as graph:
                cli.run(args, graph)
            break
        except PackageTooLargeError as exc:
            assert exc.limit == tried[-1]
            tried.append(exc.next_limit)
    assert json.loads(capsys.readouterr().out)["created"] > 0
    assert len(tried) > 1
    assert all(b == 2 * a for a, b in itertools.pairwise(tried))


# ------------------------------------------------------------ streamed import


def test_streamed_import_matches_the_in_memory_import(home, tmp_path):
    records = corpus()
    package = package_of("src", records, tmp_path)
    with KG("plain", create=True) as plain:
        whole = plain.import_package(package)
        expected = snapshot(plain)
    with KG("streamed", create=True) as streamed:
        report = streamed.import_package(package, batch_records=BATCH)
        assert snapshot(streamed) == expected
    assert "streamed" not in whole
    assert report["streamed"]["batch_records"] == BATCH
    assert report["streamed"]["batches"] >= 5
    for field in ("nodes", "unique_nodes", "edges", "created", "updated", "mode"):
        assert report[field] == whole[field]
    assert report["graph_store"].endswith("streamed.oxi")
    assert leftovers(home) == []


def test_streamed_merge_keeps_existing_records_and_survives_reopen(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    with KG("dst", create=True) as graph:
        graph.load([thing("mine", keep=True)], [link("mine", "mine", rel="SELF")])
        report = graph.import_package(package, batch_records=BATCH)
        assert report["mode"] == "merge"
        assert graph.exists("Thing", "mine") and graph.exists("Thing", "t9")
        before = snapshot(graph)
    with KG("dst") as graph:  # reopened from the swapped files
        assert snapshot(graph) == before
        assert graph.path("Thing", "t0", "Thing", "t9", ["NEXT"])[-1] == "t9"
        graph.load([thing("later")], [])  # still writable afterwards
        assert graph.exists("Thing", "later")


def test_streamed_reimport_is_idempotent(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    with KG("dst", create=True) as graph:
        graph.import_package(package, batch_records=BATCH)
        before = snapshot(graph)
        report = graph.import_package(package, batch_records=BATCH)
        assert report["created"] == report["updated"] == 0
        assert snapshot(graph) == before


def test_streamed_replace_discards_the_old_graph(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    with KG("plain", create=True) as plain:
        plain.load([thing("old")], [])
        plain.import_package(package, replace=True)
        expected = snapshot(plain)
    with KG("dst", create=True) as graph:
        graph.load([thing("old")], [])
        report = graph.import_package(package, replace=True, batch_records=BATCH)
        assert report["mode"] == "replace" and "streamed" in report
        assert not graph.exists("Thing", "old")
        assert snapshot(graph) == expected


def test_conflict_in_a_late_batch_changes_nothing(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    with KG("dst", create=True) as graph:
        graph.load([thing("t8", n=-1)], [])  # conflicts with the 9th node
        before = snapshot(graph)
        revision = graph._meta("revision")
        with pytest.raises(GraphValidationError, match="property conflict"):
            graph.import_package(package, batch_records=BATCH)
        assert snapshot(graph) == before
        assert graph._meta("revision") == revision
    with KG("dst") as graph:
        assert snapshot(graph) == before
    assert leftovers(home) == []


def test_streamed_conflict_policy_incoming_overwrites(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    with KG("dst", create=True) as graph:
        graph.load([thing("t8", n=-1)], [])
        report = graph.import_package(
            package, on_conflict="incoming", batch_records=BATCH
        )
        assert report["updated"] == 1
        assert graph.props("Thing", "t8")["n"] == 8


def refuse_staging(*_args, **_kwargs):
    raise AssertionError("the graph was copied for staging")


def test_streamed_dry_run_reports_and_writes_nothing(home, tmp_path, monkeypatch):
    package = package_of("src", corpus(), tmp_path)
    with KG("plain", create=True) as plain:
        plain.load([thing("t8", n=-1)], [])
        whole = plain.import_package(package, dry_run=True)
    with KG("dst", create=True) as graph:
        graph.load([thing("t8", n=-1)], [])
        before = snapshot(graph)
        monkeypatch.setattr(KG, "_stage_copy", refuse_staging)
        report = graph.import_package(package, dry_run=True, batch_records=BATCH)
        assert report["dry_run"] is True
        for field in ("created", "updated", "removed", "conflicts", "nodes", "edges"):
            assert report[field] == whole[field]
        assert [c["field"] for c in report["conflicts"]] == ["n"]
        assert snapshot(graph) == before
    assert not (home / "blobs" / "dst").exists()
    assert leftovers(home) == []


def test_streamed_memory_only_matches_the_in_memory_selection(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    with KG("plain", create=True) as plain:
        whole = plain.import_package(package, memory_only=True)
        expected = snapshot(plain)
    with KG("dst", create=True) as graph:
        report = graph.import_package(package, memory_only=True, batch_records=1)
        assert snapshot(graph) == expected
    assert report["memory_only"] == whole["memory_only"]
    assert report["nodes"] == whole["nodes"] and report["edges"] == whole["edges"]


def test_streamed_usage_matches_the_in_memory_import(home, tmp_path):
    package = package_of(
        "src", corpus(), tmp_path, use=[("Thing", "t1")] * 3 + [("Lesson", "l1")]
    )

    def used(graph):
        return {
            row[0]: (row[1], row[2])
            for row in graph.content.execute("SELECT h, hits, reads FROM usage")
        }

    with KG("plain", create=True) as plain:
        plain.import_package(package)
        expected = used(plain)
    with KG("dst", create=True) as graph:
        report = graph.import_package(package, batch_records=1)
        assert used(graph) == expected
    assert expected[usage_key("Thing", "t1")] == (3, 0)
    assert report["usage"] == {"rows": 2, "merged": 2}


def test_streamed_import_never_materializes_the_package(home, tmp_path, monkeypatch):
    package = package_of("src", corpus(count=40), tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("streamed import read the whole package")

    sizes = []
    merge = KG._merge_records

    def spy(self, kind, records, *args, **kwargs):
        records = list(records)
        sizes.append(len(records))
        return merge(self, kind, records, *args, **kwargs)

    monkeypatch.setattr(KG, "read_package", staticmethod(refuse))
    monkeypatch.setattr(KG, "_merge_records", spy)
    with KG("dst", create=True) as graph:
        report = graph.import_package(package, batch_records=7)
    assert max(sizes) <= 7
    # the probe's first batch, then every record once in the stage
    assert sum(sizes) == 7 + report["nodes"] + report["edges"]


def test_small_packages_keep_the_in_memory_path(home, tmp_path, monkeypatch):
    package = package_of("src", corpus(count=2), tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a small package was staged")

    monkeypatch.setattr(KG, "_stage_copy", refuse)
    with KG("dst", create=True) as graph:
        assert "streamed" not in graph.import_package(package)


def test_default_batch_size_follows_the_module_constant(home, tmp_path, monkeypatch):
    package = package_of("src", corpus(), tmp_path)
    monkeypatch.setattr(store, "IMPORT_BATCH_RECORDS", 4)
    with KG("dst", create=True) as graph:
        assert graph.import_package(package)["streamed"]["batch_records"] == 4


# ------------------------------------------------- package-level validation


def rewrite(package, tmp_path, edit, name="bad.graph-skill-db", fix_manifest=True):
    """Copy a package, editing member lines; keeps checksums valid unless told not to."""
    with zipfile.ZipFile(package) as archive:
        members = {item: archive.read(item) for item in archive.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    for member in ("nodes.jsonl", "edges.jsonl"):
        lines = members[member].decode().splitlines(keepends=True)
        members[member] = "".join(edit(member, lines)).encode()
        if fix_manifest:
            manifest["members"][member] = {
                "bytes": len(members[member]),
                "sha256": hashlib.sha256(members[member]).hexdigest(),
            }
    bad = tmp_path / name
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for member, body in members.items():
            archive.writestr(member, body)
    return bad


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (  # the same node twice with different props, in different batches
            lambda m, lines: (
                lines + [json.dumps(thing("t0", n=99)) + "\n"]
                if m == "nodes.jsonl"
                else lines
            ),
            "conflicting duplicate node",
        ),
        (
            lambda m, lines: (
                lines + [json.dumps(link("t0", "ghost")) + "\n"]
                if m == "edges.jsonl"
                else lines
            ),
            "missing endpoint",
        ),
        (
            lambda m, lines: (
                lines + [json.dumps(link("t0", "t1", weight=-5)) + "\n"]
                if m == "edges.jsonl"
                else lines
            ),
            "conflicting duplicate edge",
        ),
        (
            lambda m, lines: (
                lines + ['{"label": "Thing", "id": "x", "id": "y"}\n']
                if m == "nodes.jsonl"
                else lines
            ),
            "duplicate JSON key",
        ),
        (
            lambda m, lines: lines[:-1] if m == "edges.jsonl" else lines,
            "count mismatch",
        ),
    ],
)
def test_streamed_validation_rejects_bad_packages_before_writing(
    home, tmp_path, edit, message
):
    package = package_of("src", corpus(), tmp_path)
    bad = rewrite(package, tmp_path, edit)
    with KG("dst", create=True) as graph:
        graph.load([thing("mine")], [])
        before = snapshot(graph)
        with pytest.raises(GraphValidationError, match=message):
            graph.import_package(bad, batch_records=BATCH)
        assert snapshot(graph) == before
    assert leftovers(home) == []


def test_streamed_import_rejects_tampered_members(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    bad = rewrite(
        package,
        tmp_path,
        lambda m, lines: [line.replace('"t3"', '"x3"') for line in lines],
        fix_manifest=False,
    )
    with KG("dst", create=True) as graph:
        with pytest.raises(GraphValidationError, match="checksum mismatch"):
            graph.import_package(bad, batch_records=BATCH)
        assert graph.export_records() == ([], [])


def test_streamed_import_keeps_records_with_unicode_line_separators(home, tmp_path):
    """Canonical JSON leaves U+2028 unescaped; a line splitter that honours it
    would cut the record in two."""
    nodes, edges = corpus()
    nodes.append(thing("sep", text="before\u2028after\u2029end\x85"))
    package = package_of("src", (nodes, edges), tmp_path)
    with KG("dst", create=True) as graph:
        graph.import_package(package, batch_records=BATCH)
        assert graph.props("Thing", "sep")["text"] == "before\u2028after\u2029end\x85"


def test_in_memory_import_keeps_records_with_unicode_line_separators(home, tmp_path):
    """Same contract on the in-memory path: records and usage rows alike."""
    text = "before\u2028after\u2029end\x85"
    nodes, edges = corpus(count=2)
    nodes.append(thing("sep\u2028id", text=text))
    package = package_of(
        "src", (nodes, edges), tmp_path, use=[("Thing", "sep\u2028id")] * 2
    )
    with KG("dst", create=True) as graph:
        report = graph.import_package(package)
        assert "streamed" not in report
        assert graph.props("Thing", "sep\u2028id")["text"] == text
        assert report["usage"] == {"rows": 1, "merged": 1}
    manifest, read_nodes, _edges = KG.read_package(package)
    assert manifest["counts"]["nodes"] == len(read_nodes)


# ------------------------------------------------------------- swap safety


def test_interrupted_swap_reopens_with_the_imported_records(
    home, tmp_path, monkeypatch
):
    """SQLite is swapped first; a crash before the RDF index follows must make
    the next open rebuild the index from the new records, not trust the old one."""
    package = package_of("src", corpus(), tmp_path)
    with KG("plain", create=True) as plain:
        plain.import_package(package)
        expected = snapshot(plain)

    def crash(*_args, **_kwargs):
        raise KeyboardInterrupt("power cut")

    monkeypatch.setattr(store, "_swap_rdf", crash)
    graph = KG("dst", create=True)
    with pytest.raises(KeyboardInterrupt):
        graph.import_package(package, batch_records=BATCH)
    graph.close()
    monkeypatch.undo()
    monkeypatch.setenv("KG_HOME", str(home))
    monkeypatch.setenv("KG_USAGE", "0")
    with KG("dst") as graph:
        assert snapshot(graph) == expected


def test_stale_staging_from_a_killed_import_is_cleared(home, tmp_path):
    package = package_of("src", corpus(), tmp_path)
    with KG("dst", create=True) as graph:
        graph.load([thing("mine")], [])
    for leftover in (
        home / "rdf" / "dst.import-0123456789ab.oxi",
        home / "rdf" / "dst.oxi.old-0123456789ab",
    ):
        leftover.mkdir(parents=True)
        (leftover / "CURRENT").write_text("junk")
    (home / "content" / "dst.import-0123456789ab.sqlite3").write_text("junk")
    (home / "content" / "other.import-0123456789ab.sqlite3").write_text("keep")
    with KG("dst") as graph:
        graph.import_package(package, batch_records=BATCH)
    assert leftovers(home) == ["other.import-0123456789ab.sqlite3"]


# ------------------------------------------------------- bounded resources


def big_corpus(count=300, size=4000):
    nodes = [
        {
            "label": "Chunk",
            "id": f"c{i}",
            "props": {"source": "src", "path": "doc.md", "text": f"{i} " + "x" * size},
        }
        for i in range(count)
    ]
    nodes.append({"label": "Source", "id": "src", "props": {}})
    return nodes, []


def python_peak(action):
    """Peak bytes Python allocated while `action` ran (SQLite's own memory excluded)."""
    import tracemalloc

    tracemalloc.start()
    try:
        action()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_export_and_inspect_stream_instead_of_holding_the_package(home, tmp_path):
    records = big_corpus()  # about 1.2 MB of chunk text
    with KG("src", create=True) as graph:
        graph.load(*records)
        expected = graph.export_records()
        out = tmp_path / "p.graph-skill-db"
        assert python_peak(lambda: graph.export_package(out)) < 600_000
    assert python_peak(lambda: KG.check_package(out)) < 600_000
    manifest, scan = KG.check_package(out)
    assert (scan["nodes"], scan["edges"]) == (301, 0)
    assert manifest["counts"] == {"nodes": 301, "edges": 0}
    with KG("dst", create=True) as graph:
        graph.import_package(out)
        assert graph.export_records() == expected
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("p.")] == [out.name]


def test_cli_inspect_reports_counts_without_reading_the_package_whole(
    home, tmp_path, capsys, monkeypatch
):
    package = package_of("src", corpus(), tmp_path)
    monkeypatch.setattr(KG, "read_package", staticmethod(refuse_staging))
    cli.standalone(cli.build_parser().parse_args(["inspect", "--file", package]))
    report = json.loads(capsys.readouterr().out)
    assert (report["read_nodes"], report["read_edges"]) == (13, 10)


def test_failed_export_leaves_no_partial_file(home, tmp_path):
    with KG("src", create=True) as graph:
        graph.load(*big_corpus(count=20))
        with pytest.raises(PackageTooLargeError):
            graph.export_package(tmp_path / "p", max_bytes=1000)
    assert list(tmp_path.iterdir()) == [tmp_path / "home"]


def test_batches_are_also_bounded_in_bytes(home, tmp_path, monkeypatch):
    package = package_of("src", big_corpus(count=40), tmp_path)
    monkeypatch.setattr(store, "IMPORT_BATCH_BYTES", 20_000)  # about 4 records
    sizes = []
    merge = KG._merge_records

    def spy(self, kind, records, *args, **kwargs):
        sizes.append(len(records))
        return merge(self, kind, records, *args, **kwargs)

    monkeypatch.setattr(KG, "_merge_records", spy)
    with KG("dst", create=True) as graph:
        # few records, but too many bytes for one batch: streamed anyway
        report = graph.import_package(package, batch_records=1000)
        assert graph.exists("Chunk", "c39")
    assert "streamed" in report
    assert 1 <= max(sizes) <= 5
    assert sum(sizes) >= 41


def test_unchanged_reimport_neither_stages_nor_swaps(home, tmp_path, monkeypatch):
    package = package_of("src", corpus(), tmp_path, use=[("Thing", "t1")])
    with KG("dst", create=True) as graph:
        graph.import_package(package, batch_records=BATCH)
        before = snapshot(graph)
        stat = graph.content_path.stat()
        monkeypatch.setattr(KG, "_stage_copy", refuse_staging)
        report = graph.import_package(package, batch_records=BATCH)
        assert report["created"] == report["updated"] == 0
        assert report["usage"]["merged"] == 1
        assert snapshot(graph) == before
        after = graph.content_path.stat()
    assert after.st_ino == stat.st_ino
    assert leftovers(home) == []


def test_corrupt_stage_is_kept_and_never_swapped_in(home, tmp_path, monkeypatch):
    package = package_of("src", corpus(), tmp_path)
    monkeypatch.setattr(KG, "_quick_check", lambda self: "*** page 7: corrupt")
    with KG("dst", create=True) as graph:
        graph.load([thing("mine")], [])
        before = snapshot(graph)
        with pytest.raises(store.GraphStateError, match="quick_check") as caught:
            graph.import_package(package, batch_records=BATCH)
        assert snapshot(graph) == before
    kept = leftovers(home)
    assert any(name.endswith(".sqlite3") for name in kept)
    assert any(name in str(caught.value) for name in kept)


def corrupt_sqlite(path):
    """Overwrite every page after the header page, as a broken sync or disk would.

    The file change counter is bumped too, so an open connection drops its page
    cache and reads the damage instead of serving stale good pages.
    """
    with open(path, "r+b") as handle:
        data = bytearray(handle.read())
        page = int.from_bytes(data[16:18], "big") or 65536
        counter = int.from_bytes(data[24:28], "big") + 1
        data[24:28] = counter.to_bytes(4, "big")
        data[page:] = b"\xa5" * (len(data) - page)
        handle.seek(0)
        handle.write(data)


def test_stage_corrupted_before_compaction_is_kept_and_never_swapped_in(
    home, tmp_path, monkeypatch
):
    # Regression: VACUUM in `_compact_after` raised "database disk image is
    # malformed"; the stage was deleted and the raw sqlite3 error escaped.
    package = package_of("src", corpus(count=1100), tmp_path)
    finish = KG._stage_finish

    def finish_then_break(self, staged):
        finish(self, staged)
        corrupt_sqlite(self.content_path)

    monkeypatch.setattr(KG, "_stage_finish", finish_then_break)
    with KG("dst", create=True) as graph:
        graph.load([thing("mine")], [])
        before = snapshot(graph)
        with pytest.raises(
            store.GraphStateError, match="VACUUM: database disk image is malformed"
        ) as caught:
            graph.import_package(package, batch_records=500)
        assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)
        assert snapshot(graph) == before
    kept = leftovers(home)
    assert not any(".import-" in name for name in kept)
    assert any(name.endswith(".sqlite3") and ".failed-" in name for name in kept)
    assert any(name.endswith(".oxi") and ".failed-" in name for name in kept)
    assert all(name in str(caught.value) for name in kept)
    # The next import neither trips over the failed stage nor removes it.
    monkeypatch.setattr(KG, "_stage_finish", finish)
    with KG("dst") as graph:
        graph.import_package(package, batch_records=500)
        assert graph.props("Thing", "t1099")["n"] == 1099
    assert [name for name in leftovers(home) if ".failed-" in name] == kept


def test_non_corruption_stage_error_still_cleans_up(home, tmp_path, monkeypatch):
    package = package_of("src", corpus(), tmp_path)

    def disk_full(self, changed):
        raise sqlite3.OperationalError("database or disk is full")

    with KG("dst", create=True) as graph:
        graph.load([thing("mine")], [])
        before = snapshot(graph)
        monkeypatch.setattr(KG, "_compact_after", disk_full)
        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            graph.import_package(package, batch_records=BATCH)
        assert snapshot(graph) == before
    assert leftovers(home) == []


@pytest.mark.parametrize("batch", [BATCH, None])
def test_blob_store_is_reported_only_when_it_exists(home, tmp_path, batch):
    package = package_of("src", corpus(), tmp_path)
    with KG("dst", create=True) as graph:
        report = graph.import_package(package, batch_records=batch)
    assert report["blobs"]["store"] is None
    assert not (home / "blobs" / "dst").exists()
