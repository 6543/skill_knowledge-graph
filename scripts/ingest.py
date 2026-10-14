#!/usr/bin/env python3
"""Deterministically turn local raw files into the neutral graph format."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

TEXT_EXTENSIONS = {
    ".adoc",
    ".bash",
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".graphql",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".lua",
    ".md",
    ".mdx",
    ".nix",
    ".php",
    ".proto",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".scala",
    ".scss",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
    ".zig",
}
TEXT_NAMES = {"Dockerfile", "Justfile", "Makefile", "Taskfile"}
SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".kg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    "__pycache__",
    "node_modules",
    "site-packages",
    "target",
    "vendor",
}
# Virtualenvs are named freely, and an exact-match skip list misses every variant.
# Ingesting one pulls thousands of vendored files into the graph and buries the
# corpus the questions are actually about.
SKIP_PREFIXES = (".venv", "venv", ".direnv", ".cache")


def skipped_directory(name: str) -> bool:
    return name in SKIP_DIRECTORIES or name.startswith(SKIP_PREFIXES)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
ENVIRONMENT_VARIABLE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")
URL = re.compile(r"https?://[^\s<>'\")\]]+")


@dataclass(frozen=True)
class Chunk:
    heading: str
    text: str
    start_line: int
    end_line: int


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored += 1
        elif not self.ignored and tag in {"br", "div", "h1", "h2", "h3", "li", "p"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.ignored:
            self.ignored -= 1
        elif not self.ignored and tag in {"div", "h1", "h2", "h3", "li", "p"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def html_text(text: str) -> str:
    parser = HTMLText()
    parser.feed(text)
    lines = [
        re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()
    ]
    return "\n".join(line for line in lines if line)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def stream_sha256(path: Path, block: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


def url_host(url: str) -> str:
    """Corpora contain deliberately malformed URLs; a bad one must not abort an ingest."""
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def chunk_text(text: str, suffix: str, max_chars: int = 6000) -> list[Chunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    lines = text.splitlines()
    chunks = []
    buffer = []
    heading = ""
    start_line = 1

    def flush(end_line: int):
        nonlocal buffer
        value = "\n".join(buffer).strip()
        if value:
            chunks.append(Chunk(heading, value, start_line, end_line))
        buffer = []

    for line_number, line in enumerate(lines, 1):
        match = HEADING.match(line) if suffix.lower() in {".md", ".mdx"} else None
        if match and buffer:
            flush(line_number - 1)
        if match:
            heading = match.group(1).strip()
            start_line = line_number

        remaining = line
        while len(remaining) > max_chars:
            if buffer:
                flush(line_number - 1)
            chunks.append(
                Chunk(heading, remaining[:max_chars], line_number, line_number)
            )
            remaining = remaining[max_chars:]
            start_line = line_number

        prospective = len(remaining) + sum(len(item) + 1 for item in buffer)
        if buffer and prospective > max_chars:
            flush(line_number - 1)
            start_line = line_number
        if not buffer:
            start_line = line_number
        buffer.append(remaining)
    flush(len(lines))
    return chunks


class Builder:
    def __init__(self):
        self.nodes = {}
        self.edges = {}

    def node(self, label: str, identifier: str, **props):
        key = (label, identifier)
        clean = {
            name: value for name, value in props.items() if value not in (None, "")
        }
        existing = self.nodes.get(key)
        if existing and existing["props"] != clean:
            raise ValueError(f"conflicting node {label}:{identifier}")
        self.nodes[key] = {"label": label, "id": identifier, "props": clean}

    def edge(
        self, rel: str, src_label: str, src: str, dst_label: str, dst: str, **props
    ):
        key = (rel, src_label, src, dst_label, dst)
        value = {
            "rel": rel,
            "src_label": src_label,
            "src": src,
            "dst_label": dst_label,
            "dst": dst,
        }
        if props:
            value["props"] = props
        existing = self.edges.get(key)
        if existing and existing != value:
            raise ValueError(f"conflicting edge {key}")
        self.edges[key] = value

    def output(self):
        return (
            [self.nodes[key] for key in sorted(self.nodes)],
            [self.edges[key] for key in sorted(self.edges)],
        )


def discover(inputs: list[Path]):
    multiple = len(inputs) > 1
    seen = set()
    for root in sorted(path.resolve() for path in inputs):
        if not root.exists():
            raise FileNotFoundError(root)
        if root.is_file():
            if root.name in seen:
                raise ValueError(f"duplicate input name: {root.name}")
            seen.add(root.name)
            yield root, root.name
            continue
        prefix = root.name + "/" if multiple else ""
        for directory, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = sorted(
                d
                for d in directories
                if not skipped_directory(d) and not (Path(directory) / d).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if path.is_symlink() or not path.is_file():
                    continue
                relative = prefix + path.relative_to(root).as_posix()
                if relative in seen:
                    raise ValueError(f"duplicate input path: {relative}")
                seen.add(relative)
                yield path, relative


def is_text(path: Path, raw: bytes) -> bool:
    media_type = mimetypes.guess_type(path.name)[0] or ""
    if (
        media_type.startswith(("image/", "audio/", "video/"))
        or path.suffix.lower()
        in {".pdf", ".ps", ".eps", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"}
        or raw.startswith((b"%PDF-", b"PK\x03\x04", b"\x1f\x8b"))
    ):
        return False
    if b"\0" in raw[:8192]:
        return False
    if path.suffix.lower() in TEXT_EXTENSIONS or path.name in TEXT_NAMES:
        return True
    # Decode incrementally so a multi-byte character straddling the sample
    # boundary is not mistaken for binary content.
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        decoder.decode(raw[:8192])
        return True
    except UnicodeDecodeError:
        return False


def build_graph(
    inputs: list[Path],
    source_name: str,
    max_chars: int = 6000,
    max_file_bytes: int = 20_000_000,
    mentions: bool = True,
    links: bool = True,
    root_name: str | None = None,
):
    if not inputs or not source_name or max_chars <= 0 or max_file_bytes <= 0:
        raise ValueError("inputs, source name and positive size limits are required")
    # With several inputs, document paths are prefixed by each input's own name,
    # so one override name could not match them.
    if root_name is not None and (not root_name or len(inputs) != 1):
        raise ValueError("root name must be non-empty and needs exactly one input")
    builder = Builder()
    # Roots stay as given rather than resolved: an absolute path bakes one machine's
    # layout into the records, so the same corpus would export differently on every
    # machine and two people's packages could never be compared.
    roots = (
        [root_name]
        if root_name is not None
        else sorted(path.resolve().name for path in inputs)
    )
    builder.node("Source", source_name, name=source_name, roots=roots)
    for path, relative in discover(inputs):
        size = path.stat().st_size
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        digest = stream_sha256(path)
        node_id = f"{source_name}:{relative}@{digest}"
        if size > max_file_bytes:
            # Never read an oversized file into memory just to decide to skip it.
            builder.node(
                "Artifact",
                node_id,
                name=path.name,
                path=relative,
                source=source_name,
                media_type=media_type,
                size_bytes=size,
                sha256=digest,
                reason="exceeds max_file_bytes",
            )
            builder.edge("CONTAINS", "Source", source_name, "Artifact", node_id)
            continue
        raw = path.read_bytes()
        digest = sha256(raw)
        if not is_text(path, raw):
            builder.node(
                "Artifact",
                node_id,
                name=path.name,
                path=relative,
                source=source_name,
                media_type=media_type,
                size_bytes=len(raw),
                sha256=digest,
            )
            builder.edge("CONTAINS", "Source", source_name, "Artifact", node_id)
            continue

        text = raw.decode("utf-8", errors="replace")
        decoding = "utf-8-replacement" if "\ufffd" in text else "utf-8"
        extracted_html = path.suffix.lower() in {".htm", ".html"} or bool(
            re.match(r"\s*(?:<!doctype\s+html|<html\b)", text, re.IGNORECASE)
        )
        if extracted_html:
            text = html_text(text)
        builder.node(
            "Document",
            node_id,
            name=path.name,
            path=relative,
            source=source_name,
            repository=source_name,
            media_type=media_type,
            size_bytes=len(raw),
            sha256=digest,
            extractor="html-text@2" if extracted_html else "plain-text@2",
            decoding=decoding,
        )
        builder.edge("CONTAINS", "Source", source_name, "Document", node_id)
        for index, chunk in enumerate(chunk_text(text, path.suffix, max_chars)):
            chunk_digest = sha256(chunk.text.encode())
            chunk_id = f"{node_id}#chunk-{index:06d}-{chunk_digest[:16]}"
            builder.node(
                "Chunk",
                chunk_id,
                document_id=node_id,
                document_sha256=digest,
                extractor="html-text@2" if extracted_html else "plain-text@2",
                locator_basis="derived_text_lines"
                if extracted_html
                else "source_lines",
                decoding=decoding,
                heading=chunk.heading,
                path=relative,
                source=source_name,
                repository=source_name,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                sha256=chunk_digest,
                text=chunk.text,
            )
            builder.edge("HAS_CHUNK", "Document", node_id, "Chunk", chunk_id)
            if mentions:
                for variable in sorted(set(ENVIRONMENT_VARIABLE.findall(chunk.text))):
                    builder.node("EnvironmentVariable", variable, name=variable)
                    builder.edge(
                        "MENTIONS_ENV",
                        "Chunk",
                        chunk_id,
                        "EnvironmentVariable",
                        variable,
                    )
            if links:
                for url in sorted(set(URL.findall(chunk.text))):
                    builder.node("WebResource", url, url=url, host=url_host(url))
                    builder.edge("LINKS_TO", "Chunk", chunk_id, "WebResource", url)
    return builder.output()


def write_records(path: Path, records: list[dict], jsonl: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    if jsonl:
        path.write_text(
            "".join(
                json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
    else:
        path.write_text(
            json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--nodes", type=Path, required=True)
    parser.add_argument("--edges", type=Path, required=True)
    parser.add_argument("--chunk-chars", type=int, default=6000)
    parser.add_argument("--max-file-bytes", type=int, default=20_000_000)
    parser.add_argument("--jsonl", action="store_true")
    parser.add_argument(
        "--root-name",
        help="record this name as the Source root instead of the input's basename"
        " (single input only)",
    )
    parser.add_argument(
        "--no-mentions",
        action="store_true",
        help="skip generic environment-variable mention edges",
    )
    parser.add_argument(
        "--no-links", action="store_true", help="skip generic URL link edges"
    )
    args = parser.parse_args()
    nodes, edges = build_graph(
        args.inputs,
        args.source,
        args.chunk_chars,
        args.max_file_bytes,
        mentions=not args.no_mentions,
        links=not args.no_links,
        root_name=args.root_name,
    )
    write_records(args.nodes, nodes, args.jsonl)
    write_records(args.edges, edges, args.jsonl)
    print(json.dumps({"nodes": len(nodes), "edges": len(edges), "source": args.source}))


if __name__ == "__main__":
    main()
