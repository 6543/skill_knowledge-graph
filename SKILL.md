---
name: knowledge-graph
description: Build, grow, query, and exchange persistent evidence-backed knowledge graphs from any raw material - research papers, wiki articles, codebases, infrastructure config, logs, notes, web snapshots, or text extracted from PDFs, images, and video. Use this whenever material must stay queryable across sessions, when an answer needs relations that span many sources, when someone asks to turn a pile of information into knowledge, or when plain search cannot answer dependency, coverage, provenance, path, or absence questions. Use it for any `.graph-skill-db` file, which is this skill's portable graph package, and importing one merges it into what is already known. Also use it to benchmark a graph against a question set. Skip it only for a single-file lookup that direct reading answers completely.
---

# Knowledge Graph

Two connected indexes, because one store cannot do both jobs well:

- **Oxigraph** holds typed nodes, relations, scalar properties, and provenance. Traversal, joins, counts, and absence queries run here.
- **SQLite FTS5** holds chunk bodies and returns source path plus line range. Full-text ranking runs here.

Keeping large text out of RDF is what makes the graph traversable without loading it into memory, and keeping relations out of the text index is what makes multi-hop questions answerable at all.

## Setup

The skill root is the directory holding this `SKILL.md`. Run bundled commands from there.

```bash
python3 -m venv .venv-kg
.venv-kg/bin/pip install -r requirements.txt
```

Graphs live under `.kg` by default; set `KG_HOME` to put them somewhere else. One `KG_HOME` holds many named graphs.

## Workflow

1. **Inventory the sources.** Record local paths, repository revisions, URLs, and snapshot dates before ingesting. Provenance you did not capture at ingest time cannot be recovered later.
2. **Write the questions first, then the model.** Read [references/modeling.md](references/modeling.md). A small graph with explicit domain relations beats a large one full of generic `MENTIONS` edges.
3. **Ingest raw local material** into the neutral format:

```bash
.venv-kg/bin/python scripts/ingest.py SOURCE_PATHS... \
  --source corpus-name --nodes nodes.jsonl --edges edges.jsonl --jsonl
```

   This handles text, code, markup, and configuration. It records everything else as `Artifact` nodes. It deliberately stops there; see *Material this skill cannot read* below.

4. **Add domain relations** when the questions need schema, API, call, dependency, ownership, citation, incident, or authorship structure. Write a deterministic extractor that emits the same neutral records, and prefer a real parser or the current code-indexing capability over regular expressions. Never hardcode another skill's installation path.
5. **Validate, then load:**

```bash
.venv-kg/bin/python scripts/kg.py validate --nodes nodes.jsonl --edges edges.jsonl
.venv-kg/bin/python scripts/kg.py load project --nodes nodes.jsonl --edges edges.jsonl
.venv-kg/bin/python scripts/kg.py stats project
```

6. **Search content, inspect evidence, then traverse:**

```bash
.venv-kg/bin/python scripts/kg.py search project --text 'Where is authentication configured?'
.venv-kg/bin/python scripts/kg.py search project --keyword authentication --keyword timeout
.venv-kg/bin/python scripts/kg.py search project --text 'backup retention' --source ops
.venv-kg/bin/python scripts/kg.py show project --label ConfigKey --id auth.timeout
.venv-kg/bin/python scripts/kg.py neighbors project --label Service --id server --rels HAS_KEY
.venv-kg/bin/python scripts/kg.py reach project --label Service --id server --rels HAS_KEY,USED_BY
.venv-kg/bin/python scripts/kg.py path project --from-label Service --from-id server \
  --to-label Module --to-id scheduler --rels CALLS,DEPENDS_ON
.venv-kg/bin/python scripts/kg.py query project --sparql 'SELECT ?id WHERE { ?x a kg:ConfigKey ; kg:id ?id }'
```

7. **Answer from the returned path and line range.** Re-read the decisive text before a high-impact claim. Follow [references/verification.md](references/verification.md).

## Growing a graph instead of rebuilding it

Three load modes, because "refresh" means different things and the wrong one silently loses facts:

| Intent | Command | Effect |
|---|---|---|
| Rebuild everything from current sources | `load NAME --nodes … --edges …` | Clears the graph first. Deleted source material stops being findable. |
| Refresh one corpus among several | `load NAME --source ops --nodes … --edges …` | Clears only what that source contributed. The other sources are untouched. |
| Add newly learned facts | `load NAME --merge --nodes … --edges …` | Upserts node properties and adds relations. Nothing is removed. |

`--merge` resolves edge endpoints against nodes already in the store, so a batch can attach findings to chunks a previous batch loaded:

```bash
cat > finding.jsonl <<'JSON'
{"label":"Finding","id":"f-2026-05-03-webhook-panic","props":{"severity":"high","analyst":"session"}}
JSON
cat > cites.jsonl <<'JSON'
{"rel":"SUPPORTED_BY","src_label":"Finding","src":"f-2026-05-03-webhook-panic","dst_label":"Chunk","dst":"ops:logs/gitea-2026-05-03.log#chunk-000012-ab12cd34ef567890","props":{"confidence":0.9}}
JSON
.venv-kg/bin/python scripts/kg.py load project --merge --nodes finding.jsonl --edges cites.jsonl
```

Use `drop-source NAME --source ops` to retire a corpus outright. Keep the neutral JSONL inputs: the two stores are derived indexes, and a rebuild from records is always the safe recovery.

## Portable graphs: `.graph-skill-db`

A `.graph-skill-db` file is a zip holding `manifest.json`, `nodes.jsonl`, and `edges.jsonl` - the neutral records, not the binary stores, so a package survives store format changes and can be diffed and reviewed.

```bash
.venv-kg/bin/python scripts/kg.py export project --out handover.graph-skill-db
.venv-kg/bin/python scripts/kg.py inspect --file handover.graph-skill-db   # read the manifest first
.venv-kg/bin/python scripts/kg.py import project --file handover.graph-skill-db
```

**Import merges by default.** Someone handing over a graph is adding to what you know, not replacing it, and silently discarding an existing graph is the kind of loss that is only noticed much later. Use `--replace` only when the person explicitly asks to start from that package alone.

Before importing into a populated graph, run `inspect` and say what will arrive: source names, node and relation counts. If two packages use the same ID for different things, the second one wins on properties, so check the manifest's `sources` for collisions first. Round-tripping is exact: an export, a fresh import, and a re-export produce identical records.

## Material this skill cannot read

The bundled ingester covers text. It does not ship extractors for PDFs, images, audio, video, proprietary documents, or binary formats, and it should not: those extractors go stale, and the right tool depends on what is actually installed. When a source needs one:

1. **Look for an existing capability first.** Check the available skills and tools for a PDF, document, image, transcription, or data-analysis capability and use it.
2. **If none fits, ask the person** what the material contains and how they want it extracted - full text, layout-preserving text, tables, captions, per-slide notes, frame descriptions, OCR. A wrong extraction choice is expensive to notice later, and they usually know which parts matter.
3. **Then build the small extractor for that job** and run it. Treat it as throwaway tooling, not part of this skill.
4. **Feed the output back as `Chunk` nodes** attached to the original `Artifact` or `Document`, and record `extractor`, its version, and the page, slide, timestamp, or region the text came from. Derived text must stay distinguishable from text read directly off disk, because its reliability is different.

Never invent content from a file's name, size, or media type. An `Artifact` with no chunks is an honest statement that the material is present but not yet readable.

## Neutral graph contract

A JSON array or JSONL file per record type.

```json
{"label":"ConfigKey","id":"server.timeout","props":{"name":"timeout","source":"schema.json","line":42}}
{"rel":"HAS_KEY","src_label":"Service","src":"server","dst_label":"ConfigKey","dst":"server.timeout","props":{"source":"schema.json","line":42}}
```

- A node ID is unique within its label and stable across rebuilds.
- Labels, relation names, and property names use letters, digits, and underscores. `id` is reserved.
- Every edge endpoint must exist in the same batch, or already be in the store when merging.
- Relations are edges, not string properties.
- Put large text only in `Chunk.props.text`. The loader moves it to FTS5 and keeps its digest in the graph.
- Record provenance on extracted facts as well as nodes; edge properties stay queryable through `kg.py show`.

Validation runs before anything is written, so malformed input leaves the current graph untouched.

## How search behaves

`--text` strips question scaffolding and ranks what is left. `--keyword` takes exact phrases and requires all of them unless `--any` is given; `--scope chunk` demands they co-occur in one chunk, while the default file scope lets separate chunks of one file satisfy different phrases. `--source` confines results to one corpus.

Ranking weighs rare terms above common ones, scores a file by its best matching chunk rather than the sum over all of them, and counts a match in a path or heading extra. That combination is what stops the largest file in a corpus from winning every query - a real failure mode that buries the one module or runbook that answers the question.

Read the `unmatched` field in the output. A term that appears nowhere means the fact is genuinely absent; a term that matched but ranked badly is a retrieval problem. Treat results as candidates, not answers: an empty result can equally mean a missing extraction, a stale corpus, a token mismatch, or a wrong predicate.

## Benchmarking a graph

Retrieval quality changes silently when chunking, extraction, or the corpus changes, so measure it:

```bash
.venv-kg/bin/python scripts/bench.py project --questions questions.json --out report.json
```

Each question carries expected evidence derived from the sources themselves - never from an earlier run of the graph, which would only measure self-consistency. `scripts/bench.py` documents the question schema and reports accuracy, mean reciprocal rank, per-category breakdown, latency, and every miss. Cover simple retrieval, administration, cross-source, aggregation, absence, and programming questions; a benchmark that is all easy lookups will not notice a ranking regression.

## Scale and maintenance

Prefer JSONL for large graphs. Chunk on document structure and bounded size. Exclude generated output, dependency caches, virtual environments, and repository metadata unless the questions need them. Read [references/operations.md](references/operations.md) before changing storage, sharding a large extraction, or diagnosing a damaged index.
