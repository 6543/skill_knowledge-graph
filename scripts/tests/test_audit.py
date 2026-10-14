"""Independent regressions found while auditing the user-refined package."""

import json
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from store import KG, STATE_GRAPH, GraphValidationError


def node(identifier, label="Thing", **props):
    return {"label": label, "id": identifier, "props": props}


@pytest.fixture
def kg(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    graph = KG("audit", create=True)
    yield graph
    if hasattr(graph, "close"):
        graph.close()


def test_default_load_is_additive(kg):
    kg.load([node("old")], [])
    kg.load([node("new")], [])
    assert kg.exists("Thing", "old")


def test_partial_update_preserves_properties_and_detects_conflict(kg):
    kg.load([node("x", status="draft", evidence="source:1")], [])
    kg.load([node("x", confidence=0.8)], [], replace=False)
    assert kg.props("Thing", "x")["evidence"] == "source:1"
    before = kg.export_records()
    with pytest.raises(GraphValidationError, match="conflict"):
        kg.load([node("x", status="confirmed")], [], replace=False)
    assert kg.export_records() == before


def test_json_types_survive_export_and_import(kg, tmp_path):
    props = {
        "items": [1, False, None, {"a": []}],
        "mapping": {},
        "nil": None,
        "empty": "",
        "zero": 0,
        "flag": False,
        "value": 1.5,
    }
    kg.load([node("x", **props)], [])
    records, _ = kg.export_records()
    assert records[0]["props"] == props
    package = kg.export_package(tmp_path / "types")["package"]
    target = KG("typed", create=True)
    try:
        target.import_package(package)
        assert target.export_records() == kg.export_records()
    finally:
        if hasattr(target, "close"):
            target.close()


def test_rejected_batch_never_clears_graph(kg):
    kg.load([node("retained")], [])
    bad = node("bad", label="Chunk", text="body", start_line={"bad": "type"})
    with pytest.raises((GraphValidationError, ValueError, TypeError)):
        kg.load([bad], [], replace=True)
    assert kg.exists("Thing", "retained")


def test_names_cannot_escape_storage_root(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with pytest.raises(GraphValidationError):
        KG("../escaped", create=True)


def test_namespace_survives_reopen(kg):
    # The instance must also reject reopening an existing graph with a different namespace.
    alternate = KG("namespace", ns="urn:papers:", create=True)
    alternate.load([node("x")], [])
    if hasattr(alternate, "close"):
        alternate.close()
    else:
        alternate.content.close()
        del alternate
    reopened = KG("namespace")
    try:
        assert reopened.exists("Thing", "x")
    finally:
        if hasattr(reopened, "close"):
            reopened.close()


def test_file_search_never_joins_different_sources(kg):
    kg.load(
        [
            node(
                "a",
                "Chunk",
                source="alpha",
                document_id="same",
                path="notes",
                text="neutrino",
            ),
            node(
                "b",
                "Chunk",
                source="beta",
                document_id="same",
                path="notes",
                text="perfectoid",
            ),
        ],
        [],
    )
    assert kg.search_keywords(["neutrino", "perfectoid"]) == []


def test_file_search_never_joins_different_revisions(kg):
    kg.load(
        [
            node(
                "a",
                "Chunk",
                source="alpha",
                revision="1",
                document_id="same",
                path="notes",
                text="neutrino",
            ),
            node(
                "b",
                "Chunk",
                source="alpha",
                revision="2",
                document_id="same",
                path="notes",
                text="perfectoid",
            ),
        ],
        [],
    )
    assert kg.search_keywords(["neutrino", "perfectoid"]) == []


def test_search_returns_matched_span_and_media_locator(kg):
    kg.load(
        [
            node(
                "a",
                "Chunk",
                text="prefix " * 500 + "neutrino tail",
                page=7,
                bbox=[1, 2, 3, 4],
            )
        ],
        [],
    )
    hit = kg.search_keywords(["neutrino"])[0]["evidence"][0]
    assert "neutrino" in hit["text"]
    assert hit["page"] == 7
    assert hit["bbox"] == [1, 2, 3, 4]


def test_location_only_hit_has_evidence(kg):
    kg.load([node("a", "Chunk", path="neutrino.md", text="some body")], [])
    assert kg.search_keywords(["neutrino"])[0]["evidence"]


def test_unicode_question(kg):
    kg.load([node("a", "Chunk", text="研究 实验 结果")], [])
    assert kg.search_question("研究")


def test_source_filter_controls_unmatched(kg):
    kg.load(
        [
            node("a", "Chunk", source="a", text="neutrino"),
            node("b", "Chunk", source="b", text="perfectoid"),
        ],
        [],
    )
    assert kg.search(["neutrino"], source="b")["unmatched"] == ["neutrino"]


def test_extra_archive_members_are_rejected(kg, tmp_path):
    kg.load([node("x")], [])
    path = kg.export_package(tmp_path / "untrusted")["package"]
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("../outside", "unexpected")
    with pytest.raises(GraphValidationError):
        KG.read_package(path)


def test_manifest_count_tampering_is_rejected(kg, tmp_path):
    kg.load([node("x")], [])
    path = kg.export_package(tmp_path / "original")["package"]
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    manifest["counts"]["nodes"] = 999
    members["manifest.json"] = json.dumps(manifest)
    bad = tmp_path / "bad.graph-skill-db"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    with pytest.raises(GraphValidationError):
        KG.read_package(bad)


def test_zero_hop_path(kg):
    kg.load([node("x")], [])
    assert kg.path("Thing", "x", "Thing", "x", ["LINKS"], max_hops=0) == ["x"]


def test_sql_failure_rolls_back_records_and_text(kg):
    kg.load([node("old", "Chunk", text="original")], [])
    before = kg.export_records()
    kg.content.execute("""CREATE TRIGGER audit_failure BEFORE INSERT ON chunks
                          WHEN new.id='bad' BEGIN SELECT RAISE(ABORT,'test fault'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        kg.load([node("good"), node("bad", "Chunk", text="new text")], [])
    assert kg.export_records() == before
    assert kg.search_keywords(["original"])


def _fault(monkeypatch, kg, attribute):
    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated interrupted index update")
        yield

    monkeypatch.setattr(kg, attribute, fail)


@pytest.mark.parametrize(
    "attribute,replace", [("_quads", True), ("_record_quads", False)]
)
def test_interrupted_rdf_update_recovers_on_reopen(kg, monkeypatch, attribute, replace):
    """Both index paths must be recoverable: the bulk rebuild a replacement uses
    and the incremental delta a merge uses. Records are authoritative, so a
    half-applied index has to be detected and rebuilt rather than queried."""
    kg.load([node("old")], [])
    _fault(monkeypatch, kg, attribute)

    with pytest.raises(RuntimeError):
        kg.load([node("new", "Chunk", text="neutrino")], [], replace=replace)
    kg.close()

    with KG("audit") as reopened:
        assert reopened.search_keywords(["neutrino"])
        assert reopened.q("SELECT (COUNT(?x) AS ?n) WHERE {?x a kg:Chunk}") == [1]
        assert reopened.exists("Thing", "old") is not replace


def test_an_incremental_update_leaves_the_index_identical_to_a_rebuild(kg):
    """The delta is an optimisation over the rebuild, so it has to agree with the
    rebuild exactly, including after a source refresh removes records."""
    kg.load([node("a"), node("b", "Chunk", text="alpha")], [])
    kg.load(
        [node("c")],
        [
            {
                "rel": "LINKS",
                "src_label": "Thing",
                "src": "a",
                "dst_label": "Thing",
                "dst": "c",
                "props": {"confidence": 0.5},
            }
        ],
    )
    kg.load([node("d", source="ops")], [], source="ops")
    kg.drop_source("ops")
    data = lambda: sorted(  # noqa: E731  the revision stamp is not data
        str(quad) for quad in kg.store if quad.graph_name != STATE_GRAPH
    )
    incremental = data()

    kg.store.clear()
    kg.store.bulk_extend(kg._quads())
    assert data() == incremental


def test_edge_conflict_and_partial_properties(kg):
    edge = {
        "rel": "LINKS",
        "src_label": "Thing",
        "src": "a",
        "dst_label": "Thing",
        "dst": "b",
        "props": {"source": "paper", "confidence": 0.7},
    }
    kg.load([node("a"), node("b")], [edge])
    kg.load([], [{**edge, "props": {"page": 7}}])
    assert kg.edge_props("LINKS", "Thing", "a", "Thing", "b")["source"] == "paper"
    before = kg.export_records()
    with pytest.raises(GraphValidationError, match="conflict"):
        kg.load([], [{**edge, "props": {"confidence": 0.9}}])
    assert kg.export_records() == before


def test_source_refresh_rejects_foreign_changes(kg):
    kg.load([node("x", source="a")], [])
    with pytest.raises(GraphValidationError, match="another source"):
        kg.load([node("y", source="b")], [], source="a")
    assert kg.exists("Thing", "x")


def test_source_refresh_resolves_surviving_external_endpoints(kg):
    kg.load([node("x", source="a"), node("global")], [])
    edge = {
        "rel": "LINKS",
        "src_label": "Thing",
        "src": "new",
        "dst_label": "Thing",
        "dst": "global",
    }
    kg.load([node("new", source="a")], [edge], source="a")
    assert kg.path("Thing", "new", "Thing", "global", ["LINKS"]) == ["new", "global"]


def test_package_reimport_is_idempotent_and_allows_later_growth(kg, tmp_path):
    kg.load([node("x", "Chunk", text="neutrino", page=4)], [])
    package = kg.export_package(tmp_path / "initial")["package"]
    before = kg.export_records()
    kg.import_package(package)
    kg.import_package(package)
    assert kg.export_records() == before
    kg.load([node("later")], [])
    assert kg.exists("Chunk", "x") and kg.exists("Thing", "later")


def test_checksum_and_zip_limits(kg, tmp_path):
    kg.load([node("x")], [])
    path = kg.export_package(tmp_path / "initial")["package"]
    with pytest.raises(GraphValidationError, match="max_bytes"):
        KG.read_package(path, max_bytes=1)
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["nodes.jsonl"] = members["nodes.jsonl"].replace(b'"x"', b'"y"')
    bad = tmp_path / "bad.graph-skill-db"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    with pytest.raises(GraphValidationError, match="checksum"):
        kg.import_package(bad)
    assert kg.exists("Thing", "x") and not kg.exists("Thing", "y")


def test_invalid_json_values_and_reserved_names(kg):
    for record in [
        node("a", bad=float("nan")),
        node("b", label="_edge"),
        node("c", metadata={3: "not a string key"}),
    ]:
        with pytest.raises(GraphValidationError):
            kg.load([record], [])
    assert kg.stats()["nodes"] == 0


def test_capped_search_discloses_candidate_loss(kg):
    kg.load(
        [node(str(i), "Chunk", text="neutrino", path=f"{i}.md") for i in range(4)], []
    )
    result = kg.search(["neutrino"], candidate_limit=1)
    assert result["capped_terms"] == ["neutrino"]


def test_source_filter_applies_before_candidate_limit(kg):
    kg.load(
        [node(str(i), "Chunk", text="neutrino", source="a") for i in range(4)]
        + [node("z", "Chunk", text="neutrino", source="z")],
        [],
    )
    result = kg.search(["neutrino"], source="z", candidate_limit=1)
    assert result["results"][0]["repository"] == "z"
    assert result["capped_terms"] == []


def test_cli_add_import_export_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    script = str(Path(__file__).parents[1] / "kg.py")
    nodes = tmp_path / "nodes.jsonl"
    edges = tmp_path / "edges.jsonl"
    edges.write_text("")

    def run(*args):
        result = subprocess.run(
            [sys.executable, script, *args], capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)

    for identifier in ("old", "new"):
        nodes.write_text(json.dumps(node(identifier)) + "\n")
        run("load", "cli", "--nodes", str(nodes), "--edges", str(edges))
    assert run("stats", "cli")["nodes"] == 2
    package = run("export", "cli", "--out", str(tmp_path / "handover"))["package"]
    assert run("inspect", "--file", package)["read_nodes"] == 2
    run("import", "second", "--file", package)
    assert run("stats", "second")["nodes"] == 2


def test_cli_load_accepts_node_only_delta_without_edges(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    script = str(Path(__file__).parents[1] / "kg.py")
    nodes = tmp_path / "nodes.jsonl"

    def run(*args):
        result = subprocess.run(
            [sys.executable, script, *args], capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)

    nodes.write_text(json.dumps(node("first")) + "\n")
    run("load", "delta", "--nodes", str(nodes))
    nodes.write_text(json.dumps(node("second")) + "\n")
    run("load", "delta", "--nodes", str(nodes))
    stats = run("stats", "delta")
    assert stats["nodes"] == 2
    assert stats["rels"] == {}


def test_rdf_cache_can_be_recreated_from_sqlite(kg):
    import shutil

    kg.load([node("survives", "Chunk", text="neutrino")], [])
    path = kg.graph_path
    kg.close()
    shutil.rmtree(path)
    with KG("audit") as recovered:
        assert recovered.q("SELECT (COUNT(?x) AS ?n) WHERE {?x a kg:Chunk}") == [1]
        assert recovered.search_keywords(["neutrino"])
