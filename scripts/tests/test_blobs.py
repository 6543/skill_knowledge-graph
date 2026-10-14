"""Raw artifact bytes travel in the package as a content-addressed object store."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from store import KG, GraphValidationError, resolve_blobs


@pytest.fixture
def kg(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with KG("blobs", create=True) as graph:
        yield graph


def artifact(relative, digest, size, source="corpus"):
    return {
        "label": "Artifact",
        "id": f"{source}:{relative}@{digest}",
        "props": {
            "name": Path(relative).name,
            "path": relative,
            "source": source,
            "media_type": "application/octet-stream",
            "size_bytes": size,
            "sha256": digest,
        },
    }


def raw(root, relative, payload: bytes):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return artifact(relative, hashlib.sha256(payload).hexdigest(), len(payload))


def test_resolve_blobs_matches_by_content_not_by_path(tmp_path):
    root = tmp_path / "raw"
    node = raw(root, "img/logo.png", b"\x89PNG payload")
    found = resolve_blobs([node], [root])
    assert list(found["blobs"]) == [node["props"]["sha256"]]
    assert found["missing"] == [] and found["changed"] == []


def test_resolve_blobs_reports_changed_and_missing_without_failing(tmp_path):
    root = tmp_path / "raw"
    stale = raw(root, "a.bin", b"original")
    (root / "a.bin").write_bytes(b"edited since ingest")
    gone = artifact("b.bin", "0" * 64, 3)
    found = resolve_blobs([stale, gone], [root])
    assert found["blobs"] == {}
    assert found["changed"] == ["a.bin"]
    assert found["missing"] == ["b.bin"]


def test_resolve_blobs_defers_large_artifacts_until_asked(tmp_path):
    root = tmp_path / "raw"
    node = raw(root, "big.iso", b"x" * 4096)
    deferred = resolve_blobs([node], [root], large_bytes=1024)
    assert deferred["blobs"] == {}
    assert deferred["oversized"] == [{"path": "big.iso", "size_bytes": 4096}]
    confirmed = resolve_blobs([node], [root], large_bytes=1024, include_large=True)
    assert list(confirmed["blobs"]) == [node["props"]["sha256"]]


def test_package_round_trip_restores_referenced_raw_bytes(tmp_path, monkeypatch):
    root = tmp_path / "raw"
    payload = b"binary evidence"
    node = raw(root, "img/logo.png", payload)
    digest = node["props"]["sha256"]

    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with KG("origin", create=True) as graph:
        graph.load([node], [])
        report = graph.export_package(tmp_path / "handover", raw_roots=[root])
    assert report["blobs"]["stored"] == 1

    with KG("target", create=True) as graph:
        result = graph.import_package(report["package"], blob_dir=tmp_path / "restored")
    restored = tmp_path / "restored" / digest[:2] / digest
    assert restored.read_bytes() == payload
    assert result["blobs"]["extracted"] == 1


def test_export_without_roots_or_with_no_blobs_stays_record_only(tmp_path, kg):
    root = tmp_path / "raw"
    node = raw(root, "a.bin", b"payload")
    kg.load([node], [])
    bare = kg.export_package(tmp_path / "bare")
    assert bare["blobs"]["stored"] == 0
    opted_out = kg.export_package(tmp_path / "out", raw_roots=[root], blobs=False)
    assert opted_out["blobs"]["stored"] == 0
    manifest, _nodes, _edges = KG.read_package(opted_out["package"])
    assert manifest["blobs"] == {}


def test_corrupted_blob_member_is_rejected(tmp_path, monkeypatch):
    import zipfile

    root = tmp_path / "raw"
    node = raw(root, "a.bin", b"payload")
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with KG("origin", create=True) as graph:
        graph.load([node], [])
        package = Path(
            graph.export_package(tmp_path / "p", raw_roots=[root])["package"]
        )

    tampered = tmp_path / "tampered.graph-skill-db"
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, "w") as target:
        for entry in source.infolist():
            data = source.read(entry.filename)
            if entry.filename.startswith("blobs/"):
                data = b"tampered"
            target.writestr(entry.filename, data)

    with KG("target", create=True) as graph, pytest.raises(GraphValidationError):
        graph.import_package(tampered, blob_dir=tmp_path / "restored")


def test_legacy_record_only_packages_still_import(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    with KG("origin", create=True) as graph:
        graph.load([{"label": "Thing", "id": "a", "props": {}}], [])
        package = graph.export_package(tmp_path / "legacy")["package"]
    with KG("target", create=True) as graph:
        result = graph.import_package(package)
    assert result["nodes"] == 1
    assert result["blobs"]["extracted"] == 0


def test_cli_export_import_carry_blobs(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_HOME", str(tmp_path / "db"))
    script = str(Path(__file__).parents[1] / "kg.py")
    root = tmp_path / "raw"
    node = raw(root, "a.bin", b"cli payload")
    nodes = tmp_path / "nodes.jsonl"
    nodes.write_text(json.dumps(node) + "\n")

    def run(*args):
        result = subprocess.run(
            [sys.executable, script, *args], capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)

    run("load", "cli", "--nodes", str(nodes))
    exported = run(
        "export", "cli", "--out", str(tmp_path / "p"), "--raw-root", str(root)
    )
    assert exported["blobs"]["stored"] == 1
    inspected = run("inspect", "--file", exported["package"])
    assert inspected["read_blobs"] == 1
    imported = run(
        "import",
        "second",
        "--file",
        exported["package"],
        "--blob-dir",
        str(tmp_path / "restored"),
    )
    assert imported["blobs"]["extracted"] == 1
    digest = node["props"]["sha256"]
    assert (tmp_path / "restored" / digest[:2] / digest).read_bytes() == b"cli payload"
