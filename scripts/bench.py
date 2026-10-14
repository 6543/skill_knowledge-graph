#!/usr/bin/env python3
"""Score a graph against a question set with recorded ground truth.

The point is to catch retrieval regressions the moment the model, the chunking,
or the extractor changes. Ground truth must come from the sources themselves,
never from an earlier run of this graph, or the benchmark only measures how
consistently the graph repeats itself.

Question file: a JSON array. Every item needs `id` and `question`. `mode`
selects the probe and defaults to `text`:

    {"id": "q1", "question": "Where is the backup schedule defined?",
     "expect_paths": ["systemd/backup.timer"]}

    {"id": "q2", "question": "Which file sets both limits?", "mode": "keyword",
     "keywords": ["multi_az", "storage_encrypted"], "expect_paths": ["main.tf"]}

    {"id": "q3", "question": "How many documents are loaded?", "mode": "sparql",
     "sparql": "SELECT (COUNT(?d) AS ?n) WHERE { ?d a kg:Document }",
     "expect_min_first": 100}

Optional keys: `category`, `difficulty`, `scope`, `any`, `source`,
`expect_empty` (an absent fact must return nothing), `require_lines` (the hit
must carry a line range), `expect_first`, `expect_min_rows`,
`expect_rows_contain`.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from store import KG, tokenize


def probe(item: dict, graph: KG, k: int) -> dict:
    mode = item.get("mode", "text")
    started = time.perf_counter()
    try:
        if mode == "text":
            rows = graph.search(
                tokenize(item["question"]), limit=k, source=item.get("source")
            )["results"]
        elif mode == "keyword":
            rows = graph.search(
                item["keywords"],
                limit=k,
                scope=item.get("scope", "file"),
                require_all=not item.get("any", False),
                source=item.get("source"),
            )["results"]
        elif mode == "sparql":
            rows = graph.q(item["sparql"])
        elif mode == "neighbors":
            rows = graph.neighbors(item["label"], item["node_id"])
        else:
            raise ValueError(f"unknown mode {mode!r}")
    except Exception as exc:  # a broken probe is a result, not a crash
        return {"hit": False, "rank": None, "ms": (time.perf_counter() - started) * 1000,
                "error": f"{type(exc).__name__}: {exc}"}
    elapsed = (time.perf_counter() - started) * 1000

    if mode == "sparql":
        first = rows[0] if rows else None
        if isinstance(first, list):
            first = first[0]
        ok = True
        if "expect_first" in item:
            ok = ok and first == item["expect_first"]
        if "expect_min_first" in item:
            ok = ok and isinstance(first, (int, float)) and first >= item["expect_min_first"]
        if "expect_min_rows" in item:
            ok = ok and len(rows) >= item["expect_min_rows"]
        if "expect_rows_contain" in item:
            flat = " ".join(str(row) for row in rows)
            ok = ok and all(value in flat for value in item["expect_rows_contain"])
        return {"hit": bool(ok), "rank": 1 if ok else None, "ms": elapsed,
                "detail": {"rows": len(rows), "first": first}}

    if mode == "neighbors":
        ok = len(rows) >= item.get("expect_min_rows", 1)
        return {"hit": ok, "rank": 1 if ok else None, "ms": elapsed, "detail": {"rows": len(rows)}}

    paths = [row.get("path", "") for row in rows]
    if item.get("expect_empty"):
        ok = not rows
        return {"hit": ok, "rank": 1 if ok else None, "ms": elapsed,
                "detail": {"rows": len(rows), "top": paths[:3]}}

    rank = None
    for index, path in enumerate(paths[:k], 1):
        if any(pattern in path for pattern in item.get("expect_paths", [])):
            rank = index
            break
    if rank and item.get("require_lines"):
        evidence = rows[rank - 1].get("evidence", [])
        if not any(hit.get("start_line") is not None for hit in evidence):
            rank = None
    return {"hit": rank is not None, "rank": rank, "ms": elapsed,
            "detail": {"rows": len(rows), "top": paths[:3]}}


def main():
    parser = argparse.ArgumentParser(prog="bench.py")
    parser.add_argument("graph")
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--k", type=int, default=5, help="rank cut-off for a hit")
    parser.add_argument("--label", default="run")
    args = parser.parse_args()

    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    opened = time.perf_counter()
    graph = KG(args.graph)
    open_ms = (time.perf_counter() - opened) * 1000

    results = []
    for item in questions:
        outcome = probe(item, graph, args.k)
        outcome.update(
            id=item.get("id"),
            category=item.get("category", "uncategorised"),
            difficulty=item.get("difficulty", "unrated"),
            mode=item.get("mode", "text"),
            question=item.get("question"),
        )
        results.append(outcome)

    by_category: dict[str, list[int]] = {}
    for outcome in results:
        bucket = by_category.setdefault(outcome["category"], [0, 0])
        bucket[0] += 1 if outcome["hit"] else 0
        bucket[1] += 1
    latencies = sorted(outcome["ms"] for outcome in results)
    summary = {
        "label": args.label,
        "graph": args.graph,
        "k": args.k,
        "questions": len(results),
        "hits": sum(1 for outcome in results if outcome["hit"]),
        "accuracy": round(sum(1 for o in results if o["hit"]) / max(len(results), 1), 4),
        "mrr": round(
            statistics.mean(1 / o["rank"] if o["rank"] else 0.0 for o in results) if results else 0,
            4,
        ),
        "errors": sum(1 for outcome in results if outcome.get("error")),
        "open_ms": round(open_ms, 2),
        "median_ms": round(statistics.median(latencies), 2) if latencies else 0,
        "p95_ms": round(latencies[max(int(len(latencies) * 0.95) - 1, 0)], 2) if latencies else 0,
        "by_category": {
            name: {"hits": hits, "n": total, "accuracy": round(hits / total, 3)}
            for name, (hits, total) in sorted(by_category.items())
        },
        "stats": graph.stats(),
    }
    if args.out:
        args.out.write_text(
            json.dumps({"summary": summary, "results": results}, indent=2, default=str),
            encoding="utf-8",
        )
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    misses = [o for o in results if not o["hit"]]
    if misses:
        print(f"\n{len(misses)} misses:", file=sys.stderr)
        for outcome in misses:
            print(
                f"  {outcome['id']} [{outcome['category']}] {outcome['question']!r} "
                f"-> {outcome.get('detail') or outcome.get('error')}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
