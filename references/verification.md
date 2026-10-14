# Verification

## Checks before trusting a graph

1. `kg.py validate` succeeds.
2. Re-extracting from unchanged sources produces byte-identical neutral records.
3. Node and relation counts match the source inventory wherever an exact count exists.
4. Every sampled fact resolves to its recorded path and line range.
5. Normal load/import preserves existing knowledge; explicit replacement removes retired evidence. Source refresh preserves other source-owned nodes and removes edges touching retired nodes.
6. An export, a fresh import, and a re-export produce identical records.
7. Empty results are checked against the actual labels, relations, and source coverage before being reported as absence.
8. The question set spans simple retrieval, administration, cross-source, absence, aggregation, and programming questions.

## Ground truth

Keep the question file separate from the graph. Each item holds the question, the expected source or entity, a difficulty, and stable evidence criteria. Never derive ground truth from the graph's own output; that measures self-consistency, not correctness.

When an item fails, classify it before touching code:

- missing source;
- extraction gap;
- false or ambiguous relation;
- chunk-boundary retrieval failure;
- query formulation issue;
- stale ground truth;
- genuinely absent information.

Correct stale ground truth only after reading the source directly, and keep both the original and the reviewed benchmark results in the report. A benchmark that gets edited whenever it fails stops being evidence.

## What a usable result contains

- a stable source or repository ID;
- a relative path or URL;
- a line range, section, or snapshot locator;
- enough text to support the claim;
- a revision, digest, or retrieval date when the source can change.

Re-read the decisive text before answering anything about security, operations, migration, or compatibility. For derived text - OCR, transcripts, image descriptions - also state which extractor produced it, because its reliability differs from text read straight off disk.

## Regression fixtures

The bundled tests cover these; keep them covered when changing the code:

- invalid label, relation, and property names, including the reserved `id`;
- dangling endpoints, and endpoints resolved against the store during a merge;
- conflicting duplicate IDs;
- finite JSON values, including nested objects/arrays, null, empty strings, booleans, integers, and doubles;
- edge provenance round-trip;
- text excluded from RDF and present in FTS5;
- phrases split across chunks of one document version, with no joins across sources or revisions;
- replacement removing stale chunks and facts, and `--source` leaving other corpora intact;
- additive partial property updates, atomic rejection of conflicts, and explicitly requested incoming-value corrections;
- package round-trip fidelity, idempotent reimport, continued growth, checksums, count validation, strict members, and size limits;
- reopen persistence, namespace retention, SQL rollback, and recovery from an interrupted index update on both the bulk and incremental paths;
- an incremental index update producing exactly the same quads as a full rebuild, including after a source refresh;
- virtualenvs and vendored dependency trees excluded from an ingest by prefix, not only by exact name;
- JSON and JSONL compatibility;
- ranking: a large file must not outrank the file that answers the question, a rare term must outweigh a ubiquitous one, and a path match must find a module the body never names;
- document frequency counted across the whole index rather than inferred from a capped result set;
- an incremental merge leaving the existing index intact, and a rewritten chunk dropping the text it used to hold;
- a dropped source taking its edge provenance with it, and a source with no chunks still being listed;
- oversized files recorded as artifacts without being read into memory, and source roots that carry no absolute machine path.
