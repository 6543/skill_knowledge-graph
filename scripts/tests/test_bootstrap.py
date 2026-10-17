"""Runtime bootstrap: network first, the bundled x86_64 wheel when offline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import bootstrap
from bootstrap import BootstrapError

SKILL = Path(__file__).resolve().parents[2]
MANIFEST = json.loads((SKILL / "vendor" / "wheels.json").read_text())
X86 = {
    "system": "Linux",
    "machine": "x86_64",
    "libc": "glibc",
    "libc_version": "2.39",
    "implementation": "CPython",
    "python": "3.13.0",
}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, True),
        ({"machine": "AMD64"}, True),
        ({"python": "3.10.2"}, True),
        ({"libc_version": "2.28"}, True),
        ({"machine": "aarch64"}, False),
        ({"machine": "arm64", "system": "Darwin", "libc": ""}, False),
        ({"libc": "", "libc_version": ""}, False),  # musl or unknown libc
        ({"libc_version": "2.17"}, False),
        ({"implementation": "PyPy"}, False),
        ({"python": "3.7.9"}, False),
    ],
)
def test_bundled_wheel_matches_only_linux_x86_64_glibc_cpython(change, expected):
    assert (bootstrap.select_wheel(MANIFEST, {**X86, **change}) is not None) is expected


def test_the_bundled_wheel_is_present_and_matches_its_manifest():
    for wheel in MANIFEST["wheels"]:
        bootstrap.verify(SKILL / "vendor" / "wheels" / wheel["file"], wheel["sha256"])


def test_a_corrupted_wheel_is_refused(tmp_path):
    wheel = MANIFEST["wheels"][0]
    copy = tmp_path / wheel["file"]
    data = bytearray((SKILL / "vendor" / "wheels" / wheel["file"]).read_bytes())
    data[1000] ^= 0xFF
    copy.write_bytes(bytes(data))
    with pytest.raises(BootstrapError, match="checksum"):
        bootstrap.verify(copy, wheel["sha256"])


class Runner:
    """Records pip calls; network installs fail unless told otherwise."""

    def __init__(self, network_ok=False):
        self.calls = []
        self.network_ok = network_ok

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        offline = "--no-index" in command
        ok = offline or self.network_ok
        return subprocess.CompletedProcess(command, 0 if ok else 1, "", "network down")


def plan(tmp_path, monkeypatch, tags, runner, **kwargs):
    monkeypatch.setattr(
        bootstrap, "ensure_venv", lambda venv, python: Path(sys.executable)
    )
    monkeypatch.setattr(bootstrap, "installed_version", lambda python: None)
    monkeypatch.setattr(bootstrap, "has_pip", lambda python: True)
    return bootstrap.install(
        tmp_path / "venv", tags=tags, runner=runner, verify_import=False, **kwargs
    )


def test_network_is_tried_first(tmp_path, monkeypatch):
    runner = Runner(network_ok=True)
    report = plan(tmp_path, monkeypatch, X86, runner)
    assert report["method"] == "network" and len(runner.calls) == 1
    assert "--no-index" not in runner.calls[0]


def test_without_network_the_bundled_wheel_is_installed(tmp_path, monkeypatch):
    runner = Runner(network_ok=False)
    report = plan(tmp_path, monkeypatch, X86, runner)
    assert report["method"] == "bundled"
    assert "--no-index" in runner.calls[-1]


def test_offline_skips_the_network(tmp_path, monkeypatch):
    runner = Runner(network_ok=True)
    report = plan(tmp_path, monkeypatch, X86, runner, offline=True)
    assert report["method"] == "bundled" and len(runner.calls) == 1


def test_other_architectures_offline_get_build_instructions(tmp_path, monkeypatch):
    runner = Runner(network_ok=False)
    with pytest.raises(BootstrapError) as error:
        plan(tmp_path, monkeypatch, {**X86, "machine": "aarch64"}, runner)
    message = str(error.value)
    assert "aarch64" in message and "x86_64" in message
    assert "Rust" in message and "--no-binary" in message and "nixpkgs" in message


def test_an_existing_runtime_is_left_alone(tmp_path, monkeypatch):
    runner = Runner()
    monkeypatch.setattr(
        bootstrap, "ensure_venv", lambda venv, python: Path(sys.executable)
    )
    monkeypatch.setattr(bootstrap, "installed_version", lambda python: "0.5.11")
    report = bootstrap.install(
        tmp_path / "v", tags=X86, runner=runner, verify_import=False
    )
    assert report["method"] == "already installed" and runner.calls == []


@pytest.mark.skipif(
    bootstrap.select_wheel(MANIFEST, bootstrap.platform_tags()) is None,
    reason="the bundled wheel does not fit this machine",
)
@pytest.mark.parametrize("pip", [True, False], ids=["pip", "no-pip"])
def test_real_offline_install_into_a_fresh_venv(tmp_path, pip, monkeypatch):
    """End to end, with the package index unreachable."""
    monkeypatch.setenv("PIP_INDEX_URL", "http://127.0.0.1:9/simple")
    venv = tmp_path / "venv"
    if not pip:
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
        )
        monkeypatch.setattr(bootstrap, "has_pip", lambda python: False)
    report = bootstrap.install(venv, offline=True)
    assert report["method"] == ("bundled" if pip else "bundled (unpacked, no pip)")
    assert report["pyoxigraph"] == "0.5.11"
    python = venv / "bin" / "python"
    out = subprocess.run(
        [str(python), "-c", "import pyoxigraph; print(pyoxigraph.Store())"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Store" in out.stdout or out.returncode == 0
    shutil.rmtree(venv)
