#!/usr/bin/env python3
"""Deterministically turn local raw files into the neutral graph format."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import mimetypes
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
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
}
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


def url_host(url: str) -> str:
    """Corpora contain deliberately malformed URLs; a bad one must not abort an ingest."""
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def chunk_text(text: str, suffix: str, max_chars: int = 6000) -> list[Chunk]:
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
    for root in sorted(path.resolve() for path in inputs):
        if root.is_file():
            yield root, root.name
            continue
        prefix = root.name + "/" if multiple else ""
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if any(part in SKIP_DIRECTORIES for part in path.relative_to(root).parts):
                continue
            yield path, prefix + path.relative_to(root).as_posix()


def is_text(path: Path, raw: bytes) -> bool:
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
):
    builder = Builder()
    roots = [str(path.resolve()) for path in sorted(inputs)]
    builder.node("Source", source_name, name=source_name, roots=roots)
    for path, relative in discover(inputs):
        raw = path.read_bytes()
        digest = sha256(raw)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        node_id = f"{source_name}:{relative}"
        if len(raw) > max_file_bytes or not is_text(path, raw):
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
        if path.suffix.lower() in {".htm", ".html"} or re.match(
            r"\s*(?:<!doctype\s+html|<html\b)", text, re.IGNORECASE
        ):
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
        )
        builder.edge("CONTAINS", "Source", source_name, "Document", node_id)
        for index, chunk in enumerate(chunk_text(text, path.suffix, max_chars)):
            chunk_digest = sha256(chunk.text.encode())
            chunk_id = f"{node_id}#chunk-{index:06d}-{chunk_digest[:16]}"
            builder.node(
                "Chunk",
                chunk_id,
                document_id=node_id,
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
                        "MENTIONS_ENV", "Chunk", chunk_id, "EnvironmentVariable", variable
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
    )
    write_records(args.nodes, nodes, args.jsonl)
    write_records(args.edges, edges, args.jsonl)
    print(json.dumps({"nodes": len(nodes), "edges": len(edges), "source": args.source}))


if __name__ == "__main__":
    main()
