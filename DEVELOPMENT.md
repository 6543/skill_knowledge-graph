# Maintaining the knowledge-graph skill

This file is for whoever edits the skill (for example through skill-creator). It is deliberately **not linked from `SKILL.md`**: a model using the skill never needs it, and every line it would read costs context. Keep it that way. Runtime guidance belongs in `SKILL.md` and `references/`; how the code works and how to change it safely belongs here.

## Where the skill runs

The same skill runs in two hosts, with opposite constraints. Every change must work in both.

- **ChatGPT**: often has no internet access. Nothing may depend on the network at runtime. That is why `bootstrap.py` falls back to the wheel bundled in `vendor/wheels/` (checked against its SHA-256), and why nothing downloads at import or query time. Keep every new dependency installable offline, or do without it.
- **Claude**: has the network but is limited in compute and memory. **Peak memory must stay below 7 GB for every command, on the largest package the default limit allows (2 GiB), and less is better.** That is why import, export and inspect stream, and why batches are bounded in bytes as well as records. Measure before claiming a change is fine (see "Measuring"), and treat any new whole-package or whole-graph structure in memory as a regression unless it is proven to stay well under the budget.

Current watch items against the 7 GB budget: the RDF rebuild (`Store.bulk_extend`, about 1 GiB at 7.4 million quads, growing with the graph), and `diff`, which still holds both packages in memory.

## Decisions made by the user

Decisions the user took explicitly while this version was built (2026-10-17 and 2026-10-18). Do not reverse them silently; ask the user first.

| Decision | Where it lives |
|---|---|
| Default uncompressed package limit is **2 GiB** (2 × 1024³ bytes, not 2 GB), shared by import, inspect and export (previously 512 MiB). | `MAX_PACKAGE_BYTES` |
| Over the limit, **the user is asked** before every retry, and each approval **doubles the current limit** (2 → 4 → 8 GiB); never skip a step, pick a bigger value, or raise it unasked. The model asks; `kg.py` does not prompt on a terminal (it is normally run non-interactively). | `PackageTooLargeError.next_limit`, the bold rule in `SKILL.md` |
| Import streams large packages: validation and loading in **bounded batches (50,000 records)**, never materializing the package, with an **all-or-nothing guarantee by staging into a temporary graph and swapping it in**. | `_import_streamed`, `_stage_copy`, `_swap_in` |
| **Only large packages are staged**; packages within the batch limit keep the in-memory, single-transaction import, so small session-memory imports do not copy the graph. | `_PackageStream.large`, `import_package` |
| Bugs found while testing the real `woodpecker-history` package are fixed, with the code kept **simple and not bloated**; refactoring is allowed as long as no existing functionality is lost. | the probe, byte-bounded batches, streaming export and inspect, `quick_check` |
| All code may be **formatted**, and maintainer documentation lives here, **reachable by skill-creator but not by the skill at runtime**. | `ruff.toml`, this file |
| Peak memory **below 7 GB** in Claude; **offline fallbacks** for ChatGPT. | "Where the skill runs" above |

Choices made while implementing those decisions, open to revision with good reason: the 64 MiB byte cap per batch (`IMPORT_BATCH_BYTES`), the rolled-back probe that answers dry runs and skips no-op imports, keeping failed stages as `.failed-` for diagnosis, and merging usage counters after the swap.

## Layout

| Path | Role |
|---|---|
| `SKILL.md` | Runtime instructions. Keep under about 320 lines; push detail to `references/`. |
| `references/operations.md` | Scale limits, package contract, streamed import, refresh, compaction, recovery. |
| `references/modeling.md` | Identities, relations, memory labels. |
| `references/verification.md` | How answers must be checked against sources. |
| `scripts/store.py` | Everything that touches data: the `KG` class, package reading and writing, validation. |
| `scripts/kg.py` | Thin CLI over `store.py`; argument parsing and output shaping only. |
| `scripts/ingest.py` | Turns local files into neutral records (Source, Document, Chunk, Artifact, mentions). |
| `scripts/bench.py` | Retrieval and structural probes against a question file. |
| `scripts/bootstrap.py` | Standard-library-only setup of the venv and pyoxigraph; must run on any Python 3. |
| `scripts/tests/` | pytest suite; `conftest.py` puts `scripts/` on the path. |
| `vendor/wheels/` | Offline pyoxigraph wheel, pinned by SHA-256 in `vendor/wheels.json`. |
| `ruff.toml` | Formatter and linter settings (not used at runtime). |
| `DEVELOPMENT.md` | This file. |

`store.py` is ordered top-down: constants, JSON helpers, errors, blob handling, package reading (`_package_header`, `_PackageStream`, `_PackageIndex`, `_validate_stream`, `_scanned`), staging helpers, record validation, text helpers, then `KG`. Inside `KG`, comment banners mark the sections: usage, refresh, compaction, consistency, storage, write, transfer, streamed import, read, search, remove, stats.

## Setup and checks

```bash
python3 scripts/bootstrap.py --venv .venv-kg
.venv-kg/bin/pip install pytest ruff
.venv-kg/bin/ruff format .            # format everything
.venv-kg/bin/ruff check .             # must print "All checks passed!"
.venv-kg/bin/python -m pytest -q scripts/tests
```

Run all three before handing an edit back. The baseline is clean: no lint findings, everything formatted, every test passing. `ruff.toml` pins the settings, so results do not depend on the editor's global ruff config (one machine had a much broader global rule set; that is why it is pinned). Recent ruff versions enable more rules by default than the classic `E`/`F`; the pinned file accepts that default and adds import sorting.

`bootstrap.py` keeps its `sys.version_info < (3, 10)` check with `# noqa: UP036` on purpose: on an old Python it must print a clear message instead of failing on syntax.

### Forcing the streamed import path

Most existing tests use small packages and therefore the in-memory import path. Rerun the whole suite through the streamed path after any change to import or package code:

```bash
cp -r . /tmp/kg-forced && cd /tmp/kg-forced
printf '\nimport store\nstore.IMPORT_BATCH_RECORDS = 1\n' >> scripts/tests/conftest.py
.venv-kg/bin/python -m pytest -q scripts/tests      # adjust the venv path
# then the same with: store.IMPORT_BATCH_BYTES = 200
```

Expected failures under forcing are only the tests that assert the in-memory path or exact batch sizes: `test_streamed_import_matches_the_in_memory_import`, `test_small_packages_keep_the_in_memory_path`, `test_in_memory_import_keeps_records_with_unicode_line_separators`, and with the byte cap also `test_streamed_import_never_materializes_the_package`. Anything else failing is a real regression.

## Architecture

### Two stores, one authority

- **SQLite** (`KG_HOME/content/NAME.sqlite3`) is authoritative: `records` (canonical JSON per node and edge, `WITHOUT ROWID`), `chunks` (chunk text, stored once), FTS5 indexes (`chunks_fts`, `locations_fts`, `node_fts`), `node_index`, `usage`, `metadata`. Rollback journal (`journal_mode=DELETE`), `synchronous=FULL`, 16 KiB pages. WAL was removed after a lost `-wal` file reopened a graph empty.
- **Oxigraph** (`KG_HOME/rdf/NAME.oxi`, a RocksDB directory) is a derived, rebuildable index for SPARQL, traversal and counts. Chunk text never becomes a quad.
- `metadata.revision` counts committed SQLite writes; `metadata.rdf_revision` says which revision the RDF index reflects. The RDF store also carries a stamp of that revision in the named graph `urn:graph-skill-db:state`. **SQLite always commits first.** A stamp ahead of SQLite means SQLite lost writes, and `_check_state` refuses to open the graph (`GraphStateError`) rather than show a partial graph.
- After a write, `_apply_rdf_delta` updates quads record by record while the change is small (`RDF_DELTA_RATIO`), otherwise `_ensure_rdf` rebuilds the index from SQLite.
- The ox.Store holds an exclusive lock: one process per graph. Several later invariants rely on it.

### Writes

`load` validates, normalizes, then `_load_transaction` runs one SQLite transaction; the per-record merge lives in `_merge_records` and is shared with the streamed import. A batch of at least `BULK_INDEX_MIN` nodes that is large next to the graph drops the FTS triggers inside the transaction and rebuilds the indexes once (`_bulk_index_begin` / `_bulk_index_end`); a rollback brings the triggers back.

### Packages

`.graph-skill-db` is a ZIP of `manifest.json`, `nodes.jsonl`, `edges.jsonl`, optional `usage.jsonl`, optional `blobs/<sha256>`. The full contract is in `references/operations.md`; the reader is the reference implementation.

- **Reading.** `_PackageStream` opens the zip, runs `_package_header` (members, sizes, manifest, blob set, the `--max-bytes` limit), and then reads members line by line, re-checking byte count and SHA-256 at the end of every full pass. `_scanned` verifies checksums, then runs `_validate_stream` against a scratch SQLite key index (`_PackageIndex`), so duplicate and endpoint checks never need every key in memory. `read_package` is that same scan with `collect=True`; `check_package` (CLI `inspect`) holds no records.
- **Export.** `export_package` streams `_record_lines` straight into zip members via `_write_member` (hashing and counting) and writes the manifest last. The size limit is checked once the records are written; the temporary file is deleted on any failure.
- **Limit.** `MAX_PACKAGE_BYTES` (2 GiB uncompressed, blobs included). Over it, `PackageTooLargeError` carries `next_limit = limit * 2`. `SKILL.md` makes the model ask the user before each doubling; keep the error text and that rule in sync.

### Import paths

`import_package` picks a path with `_PackageStream.large(batch)`:

1. **In memory** (at most `IMPORT_BATCH_RECORDS` records and at most `IMPORT_BATCH_BYTES` of `nodes.jsonl` plus `edges.jsonl`): `read_package`, then `load` in one transaction, then usage. Unchanged behaviour from before streaming existed.
2. **Streamed** (`_import_streamed`):
   1. `_scanned`: checksums, then record validation against the key index under `KG_HOME/.NAME.scan-*`.
   2. Probe: `_merge_package(probe=True)` merges into the live graph inside one transaction that is always rolled back. A dry run reports from the complete probe. Otherwise the probe stops at the first batch that would change something, unless it found conflicts, which it then collects completely and rejects. No change at all ends the import here, without a copy. `--replace` skips the probe unless it is a dry run, because a replacement always changes the graph and the probe's `DELETE` would journal the whole database.
   3. Stage: `_stage_copy` copies SQLite with the backup API and checkpoints RDF (`Store.backup`, hard links) to `NAME.import-<12 hex>`. `_merge_package` merges batches (records or bytes, whichever ends first) committing each batch; `_stage_finish` raises on conflicts, rebuilds FTS, bumps the revision, stamps or rebuilds RDF.
   4. Check: `PRAGMA quick_check` on the stage. A failing stage is renamed to `NAME.failed-<hex>.*` and reported; never swapped in. Cleanup never touches `.failed-` names, because a store that broke (for example during VACUUM in a synchronized folder) is evidence.
   5. Swap (`_swap_in`): stage marked `rdf_revision = SWAP_PENDING`; SQLite file replaced first; RDF directory swapped with two renames (`_swap_rdf`, which restores the old directory if the second rename fails); marker cleared only if the staged index was current; the `KG` object reopens itself.
   6. Usage counters merge into the live graph afterwards, in batches (`_import_usage`), as in the in-memory path.
3. `_clear_stale_staging` deletes leftovers of a killed import of the same graph before a streamed import starts. Safe only because the graph's RDF lock is held.

## Invariants that must survive any edit

- SQLite commits before RDF is stamped; RDF can always be rebuilt from SQLite, never the reverse.
- An import is all or nothing for records: no partial batch ever becomes visible in the live graph.
- A crash at any point of the swap leaves either the old graph or the new records with an RDF index that is current or marked for rebuild. Never new records with a stale index trusted as current.
- Streamed import, export and inspect hold at most one batch of records (plus the RDF rebuild), not the package.
- Package members are split on `\n` only, never `str.splitlines()`: canonical JSON leaves U+2028, U+2029 and U+0085 unescaped.
- Validation is identical on both import paths: every error the in-memory reader raises, the streamed reader raises too (possibly in a different order).
- Conflicts are listed completely in one error, never one per attempt.
- The byte limit is raised only with the user's consent, one doubling at a time.
- `_merge_usage` merges by maximum, so re-importing never double counts.
- Export of an imported package reproduces `nodes.jsonl`, `edges.jsonl` and `usage.jsonl` byte for byte.

## Pitfalls already hit

- **`str.splitlines`** broke packages containing U+2028 in any property; fixed in the reader, covered by tests on both paths.
- **Materializing export/inspect** used 5.5 GiB and 4.5 GiB for a 952 MB package; a package at the 2 GiB limit would not fit an 8 GiB machine. Keep both streaming.
- **Record-count-only batches**: 50k records of a few KB each already took about 900 MiB; a few huge chunks could take gigabytes. Batches end at records or bytes.
- **No-op re-import copied the whole graph** (1.8 GB, 130 s). The probe avoids it.
- **No integrity check before swap**: a stage corrupted by VACUUM would have replaced a healthy graph.
- `with self.content:` commits on exit. In probe mode `_merge_package` uses `contextlib.nullcontext()` instead and rolls back in `finally`; do not add a `with self.content:` inside the probe.
- `sqlite3` opens transactions implicitly before DML; `_bulk_index_begin` issues `BEGIN` only when none is open.
- A RocksDB directory cannot replace a non-empty directory in one rename; see `_swap_rdf`.
- `Store.backup` needs a target directory that does not exist yet.
- Graph names may be 128 characters; staging names hash long names (`_staging_prefix`) to stay under the limit, and the cleanup patterns depend on that prefix.
- Python's `tracemalloc` does not see SQLite's or RocksDB's memory. The streaming tests use it to prove that no Python-side copy of the package exists; they say nothing about native memory.
- Avoid nested-loop `break` gymnastics: `_package_batches` flattens (kind, records) so `_merge_package` has one loop.

## Testing conventions

- Write the failing test first, see it fail for the expected reason, then fix. Mutation-check new tests where cheap (break the fix, confirm the test fails).
- Isolate every graph: `monkeypatch.setenv("KG_HOME", tmp_path / "home")` and `KG_USAGE=0` unless usage is under test.
- Force the streamed path in a test with `batch_records=` on `import_package`, or by monkeypatching `store.IMPORT_BATCH_RECORDS` / `store.IMPORT_BATCH_BYTES` (both read at call time).
- Prove "never materializes" by monkeypatching `KG.read_package` to raise, and by spying on `KG._merge_records` batch sizes.
- Simulate crashes by monkeypatching `store._swap_rdf` (or another step) to raise `KeyboardInterrupt`, then reopen the graph and compare a snapshot.
- Simulate a corrupt stage by monkeypatching `KG._quick_check`.
- Compare paths by snapshot (records, triple count, SPARQL count, search and find results), not by internal tables.
- Test behaviour, contracts, regressions and edge cases; do not test declarations or third-party behaviour.

`scripts/tests/test_stream_import.py` covers the byte limit, both import paths, the probe, staging, swap safety, stale and failed stages, streaming export and inspect, and the U+2028 regression.

## Measuring

Run the CLI as a child process and read its peak memory afterwards; `/usr/bin/time` is often missing in sandboxes:

```python
import resource, subprocess, sys, time

start = time.time()
# argv: .venv-kg/bin/python scripts/kg.py import g --file p.graph-skill-db
code = subprocess.call(sys.argv[1:])
peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
print(
    f"exit={code} wall={time.time() - start:.1f}s maxrss={peak:.0f}MiB", file=sys.stderr
)
```

Reference numbers, `woodpecker-history.graph-skill-db` (314,794 nodes, 656,457 edges, 952 MB uncompressed), 2 CPUs, 8 GiB RAM, 2026-10-18:

| Command | Wall | Peak RSS |
|---|---|---|
| `import` into an empty graph (28 batches) | 240 s | 1245 MiB |
| Unchanged re-import (probe only) | 114 s | 520 MiB |
| `import --dry-run` | 124 s | 521 MiB |
| `inspect` | 62 s | 37 MiB |
| `export` | 44 s | 148 MiB |

The import peak is mostly the RDF rebuild (`Store.bulk_extend` over 7.4 million quads), not the package. A result far above these numbers on similar data is a regression.

## Known limitations and backlog

- `diff` holds both packages in memory (`read_package` plus `diff_records`).
- The RDF rebuild peaks around 1 GiB for about 7 million quads; chunked `bulk_extend` might lower it.
- `inspect` is about twice as slow as the old in-memory reader because of the on-disk key index.
- Usage counters commit after the records, not in the same transaction (both import paths). A crash in between loses only the counter merge.
- An unchanged re-import still scans and probes the whole package: no copy, but about two minutes on the reference package.
- `IMPORT_BATCH_BYTES` counts raw line bytes; parsed Python objects take several times that.
- The in-memory import path and `diff` still materialize; they are bounded by the streaming thresholds and by the inputs `diff` is given.

## Editing checklist

1. Read "Where the skill runs", "Decisions made by the user" and the section for the code you touch; preserve the invariants and stay under the 7 GB peak.
2. Tests first; then code; then the forced streamed-path runs if import or package code changed.
3. `ruff format .`, `ruff check .`, full pytest: all clean.
4. Update `SKILL.md` only for what a model using the skill must do differently; put details in `references/operations.md`; update this file for anything a maintainer must know.
5. House style for docs: plain sentences, no em dashes, imperative mood in `SKILL.md`.
6. Package with skill-creator (`python -m scripts.package_skill <skill-dir> <out-dir>`); remove `.pytest_cache`, `.ruff_cache` and `__pycache__` first, because the packager includes them.
