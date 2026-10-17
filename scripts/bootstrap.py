#!/usr/bin/env python3
"""Set up the knowledge-graph runtime, offline when the network is not there.

Standard library only: it has to run before pyoxigraph exists. Steps:

1. Check Python >= 3.10 and SQLite with FTS5 (part of Python; cannot be bundled).
2. Create the virtualenv (default .venv-kg) unless it exists.
3. Leave a working pyoxigraph 0.5 alone.
4. Install requirements.txt from the package index.
5. If that fails, or with --offline, install the wheel bundled in vendor/wheels
   after checking its SHA-256. It fits Linux x86_64 with glibc >= 2.28 and
   CPython >= 3.8. Without pip in the environment the wheel is unpacked directly.
6. Anywhere else without network: stop and say how to build it.

Usage:
    python3 scripts/bootstrap.py [--venv .venv-kg] [--offline] [--python PYTHON]

Prints one JSON report; exits 2 with "error: ..." when it cannot finish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
VENDOR = SKILL_ROOT / "vendor"
REQUIREMENTS = SKILL_ROOT / "requirements.txt"
SUPPORTED = ("0.5.",)
PIP_NETWORK = ["--disable-pip-version-check", "--timeout", "15", "--retries", "1"]


class BootstrapError(Exception):
    pass


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".") if part.isdigit())


def platform_tags() -> dict:
    libc, libc_version = platform.libc_ver()
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "libc": libc,
        "libc_version": libc_version,
        "implementation": platform.python_implementation(),
        "python": platform.python_version(),
    }


def select_wheel(manifest: dict, tags: dict) -> dict | None:
    """The bundled wheel that runs on this platform, or None."""
    for wheel in manifest.get("wheels", []):
        if (
            tags["system"] == wheel["system"]
            and tags["machine"] in wheel["machines"]
            and tags["libc"] == wheel["libc"]
            and tags["libc_version"]
            and _version(tags["libc_version"]) >= _version(wheel["min_libc_version"])
            and tags["implementation"] == wheel["implementation"]
            and _version(tags["python"]) >= _version(wheel["min_python"])
        ):
            return wheel
    return None


def verify(path: Path, sha256: str) -> None:
    if not path.is_file():
        raise BootstrapError(f"{path}: bundled wheel is missing")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != sha256:
        raise BootstrapError(f"{path}: checksum mismatch; refusing to install it")


def check_python() -> None:
    # Runs on any Python 3, so an older one gets this message, not a crash.
    if sys.version_info < (3, 10):  # noqa: UP036
        raise BootstrapError(
            f"Python 3.10+ is required, this is {platform.python_version()}"
        )
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.Error as exc:
        raise BootstrapError(
            "this Python's SQLite lacks FTS5, which cannot be bundled; use a Python "
            f"built with FTS5 (most distribution and python.org builds are): {exc}"
        ) from exc


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def ensure_venv(venv: Path, python: str) -> Path:
    target = venv_python(venv)
    if target.exists():
        return target
    made = subprocess.run(
        [python, "-m", "venv", str(venv)], capture_output=True, text=True, check=False
    )
    if made.returncode != 0:
        # Debian-style Pythons without ensurepip can still make a pip-less venv;
        # the bundled wheel is then unpacked directly.
        made = subprocess.run(
            [python, "-m", "venv", "--without-pip", str(venv)],
            capture_output=True,
            text=True,
            check=False,
        )
        if made.returncode != 0:
            raise BootstrapError(f"cannot create {venv}: {made.stderr.strip()}")
    return target


def installed_version(python: Path) -> str | None:
    probe = subprocess.run(
        [str(python), "-c", "import pyoxigraph; print(pyoxigraph.__version__)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.stdout.strip() if probe.returncode == 0 else None


def has_pip(python: Path) -> bool:
    probe = subprocess.run(
        [str(python), "-m", "pip", "--version"], capture_output=True, check=False
    )
    return probe.returncode == 0


def unpack_wheel(python: Path, wheel: Path) -> None:
    """Install a wheel without pip: a wheel is a zip laid out for site-packages."""
    where = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['platlib'])",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    target = Path(where).resolve()
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            destination = (target / name).resolve()
            if target not in destination.parents:
                raise BootstrapError(
                    f"{wheel.name}: member {name!r} escapes site-packages"
                )
        archive.extractall(target)


def guidance(tags: dict) -> str:
    where = f"{tags['system']} {tags['machine']} {tags['libc'] or 'unknown libc'}"
    return (
        f"no network and no bundled wheel for {where}: the skill bundles pyoxigraph "
        "only for Linux x86_64 with glibc >= 2.28. With network access, rerun without "
        "--offline; PyPI has prebuilt pyoxigraph wheels for Linux aarch64 (glibc and "
        "musl), macOS and Windows. Where no wheel fits, pip builds from source, which "
        "needs a Rust toolchain (rustup or cargo), maturin and access to crates.io: "
        "VENV/bin/python -m pip install --no-binary pyoxigraph 'pyoxigraph==0.5.11'. "
        "On NixOS use python3Packages.pyoxigraph from nixpkgs instead."
    )


def install(
    venv: Path,
    offline: bool = False,
    python: str = sys.executable,
    tags: dict | None = None,
    runner=subprocess.run,
    verify_import: bool = True,
) -> dict:
    check_python()
    tags = tags or platform_tags()
    target = ensure_venv(Path(venv), python)
    report = {"venv": str(venv), "python": str(target), "platform": tags}
    current = installed_version(target)
    if current and current.startswith(SUPPORTED):
        return {**report, "method": "already installed", "pyoxigraph": current}

    pip = has_pip(target)
    network_error = None
    if not offline and pip:
        result = runner(
            [
                str(target),
                "-m",
                "pip",
                "install",
                *PIP_NETWORK,
                "-r",
                str(REQUIREMENTS),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            method = "network"
        else:
            lines = (result.stderr or "").strip().splitlines()
            network_error = lines[-1] if lines else "pip failed"
            method = None
    else:
        method = None

    if method is None:
        manifest = json.loads((VENDOR / "wheels.json").read_text(encoding="utf-8"))
        wheel = select_wheel(manifest, tags)
        if wheel is None:
            raise BootstrapError(guidance(tags))
        path = VENDOR / "wheels" / wheel["file"]
        verify(path, wheel["sha256"])
        if pip:
            result = runner(
                [
                    str(target),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--find-links",
                    str(path.parent),
                    f"{wheel['distribution']}=={wheel['version']}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise BootstrapError(f"offline install failed: {result.stderr.strip()}")
            method = "bundled"
        else:
            unpack_wheel(target, path)
            method = "bundled (unpacked, no pip)"

    version = installed_version(target) if verify_import else "unverified"
    if verify_import and not version:
        raise BootstrapError(f"pyoxigraph still does not import in {target}")
    return {
        **report,
        "method": method,
        "pyoxigraph": version,
        **({"network_error": network_error} if network_error else {}),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--venv", type=Path, default=Path(".venv-kg"))
    parser.add_argument("--offline", action="store_true", help="skip the package index")
    parser.add_argument(
        "--python", default=sys.executable, help="interpreter for the venv"
    )
    args = parser.parse_args(argv)
    try:
        report = install(args.venv, offline=args.offline, python=args.python)
    except (BootstrapError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
