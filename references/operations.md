# Operations

## Durable storage and recovery

`KG_HOME` contains `content/NAME.sqlite3`, `rdf/NAME.oxi`, and `blobs/NAME/` for imported raw artifact bytes. SQLite is authoritative: neutral records, metadata, chunk bodies, usage counters, and FTS indexes commit together with a rollback journal and synchronous FULL. Oxigraph is a derived topology index. Never edit either store directly.

A committed write is in the main SQLite file when the command returns. Earlier versions used WAL, where a commit could live only in the `-wal` file until a checkpoint; a killed process plus a lost `-wal` file reopened the graph empty while the RDF index still held everything. The RDF index now records which SQLite revision it reflects, in a named graph that queries never see. If SQLite is older than the index, or empty while an unstamped index at the same revision is populated, opening fails with "older than its RDF index" and changes nothing. Do not delete files to get past it: move both stores aside and import the newest exported package. Rebuilding records from the index would lose all chunk text.

Opening a graph written by an earlier version migrates it once: chunk text is stored once, record keys are stored once (a `WITHOUT ROWID` table), pages become 16 KiB so ~1 KB records stay off overflow pages, and one `VACUUM` reclaims the space. Importing into an old graph opens it first, so it migrates too. On the woodpecker and kithara example packages this took 3.1 s and 1.3 s and cut the SQLite file from 206.9 MB to 141.2 MB and from 83.0 MB to 55.7 MB. A write that changes at least 1,000 records and a quarter of the graph (large import, `drop-source`, bulk `delete`) ends with a `VACUUM`; small deltas never pay for one.

The code acquires Oxigraph's exclusive lock before opening SQLite. Use one `KG` instance per named graph, close it, and run CLI commands sequentially. Different graphs can be used independently. Python usage:

```python
from store import KG
with KG("project", create=True) as graph:
    graph.load(nodes, edges)  # additive, conflicts abort
    answer = graph.search_question("Where is the timeout configured?")
```

Malformed batches, missing endpoints, property collisions, and SQL transaction failures leave existing records and text unchanged. RDF is updated after the canonical transaction commits, and its completed revision is recorded only after `Store.flush()`. If the process fails during that update, records may already be committed and the index is left marked stale. Reopen or query the graph to retry before using topology. Retrying the same merge is safe. Persistent I/O errors require fixing the storage problem first.

The index is moved forward by the records a write actually touched, rather than being rebuilt whole every time. A rebuild is still used for a full replacement, for the first open of a graph whose index is missing or stale, and whenever the change covers a large enough share of the graph that the bulk path is cheaper. This is what keeps adding a single fact to a large graph a sub-second operation instead of one proportional to the whole corpus; both paths produce an identical index, which the tests assert directly.

This is recoverable consistency across two engines, not a distributed transaction. A process holding `graph.store` directly can bypass recovery; use the supported KG methods. Do not copy live database files as a backup. Use `export`, or close all writers and copy the complete graph directory. Package writes use a temporary file and atomic rename so an interrupted export does not truncate an existing package.

Older local draft stores migrate on first open by reconstructing neutral records and rebuilding chunk identity/indexes. Their previously discarded or stringified properties cannot be recovered automatically. Export and preserve the old graph before upgrading when the old tool is available. Legacy namespaces are inferred only from recognizable node/type IRIs; ambiguity fails rather than silently importing an empty graph. New stores persist their namespace. Explicitly reopening with a conflicting namespace fails.

A missing RDF directory can be rebuilt from the SQLite records. For a damaged RDF directory, stop writers, preserve a copy, then move that derived directory aside and reopen. For damaged SQLite, restore an exported package into a new graph name, or re-extract from the retained sources. Verify counts, evidence, and representative queries before using the restored graph.

## Merge and ownership

Default `load` and `import` merge. Properties omitted from an incoming node or edge remain unchanged. Equal values are idempotent, including nested JSON values. Conflicting fields abort the entire batch. `--on-conflict incoming` explicitly authorizes overwriting supplied conflicting fields; it retains unspecified fields and does not preserve overwritten history. Prefer version or assertion nodes when both values are meaningful. One exception: `Source.roots` is provenance, not a fact, so it merges as a sorted set of strings. Ingesting or importing the same source from a differently named checkout adds the new root instead of aborting. Non-list values or non-string items still conflict normally.

The ingester records `Source.roots` as the basename of each input. Pass `ingest.py --root-name NAME` to record `NAME` instead, for example to keep a stable name when the checkout directory is named differently. It requires exactly one input, because with several inputs each document path is prefixed by its own input name.

A rejected batch lists every conflicting field, the first 20 in full. `load --dry-run` and `import --dry-run` run the whole batch inside the transaction and roll it back: the report gives `created`, `updated`, `removed`, and every conflict with stored and incoming values, and nothing is written (an import dry run also skips blob extraction). Use it before merging a package from another lineage.

`load --replace` replaces the whole graph. `load --source NAME` replaces that source wholesale and rejects batches tagged to other sources; it also drops every edge into the replaced versions, including evidence links. Edge endpoints may resolve against surviving external nodes. `drop-source --source NAME` retires it. These operations are explicit and destructive.

`refresh --source NAME` is the evidence-preserving way to bring a source up to date. It compares a fresh ingest of that one source with the current versions:

- A `Document` or `Artifact` the fresh batch no longer contains is stale. With its chunks it is deleted when no evidence edge touches it, and otherwise kept with `outdated: true`, `outdated_since` (UTC date) and, with `--superseded-by`, `superseded_by`.
- An unchanged document keeps its ID, but re-chunking (another chunk size or ingester version) yields new chunk IDs. Its chunks the batch no longer attaches are judged the same way (`stale_chunks`).
- Evidence is any edge whose relation is not an ingester relation (`CONTAINS`, `HAS_CHUNK`, `LINKS_TO`, `MENTIONS_ENV`). Name bookkeeping relations such as a snapshot's `INCLUDES` with `--bookkeeping-rel` so they do not keep versions alive.
- A record the batch contains again loses its `outdated` marks.
- `WebResource` and `EnvironmentVariable` nodes left without edges go too.
- The batch belongs to the one source, so its values win for that source's records (`--on-conflict incoming` is the default; a newer ingester can move the locators of unchanged text). Properties the batch does not supply are kept.

Everything commits in one transaction; without `--apply` it is a preview. Check that `evidence_edges_after` equals `evidence_edges_before`, then rerun the dry run: it must report nothing stale, created or updated. On the woodpecker example a refresh of `autoscaler` took 2.0 s and its rerun reported no changes.

`diff --file A --file B` (or `--graph NAME --file B`) compares two lineages before a merge: counts per label and relation only in A, only in B, and differing; memory notes unique to each side or conflicting by ID; and `(source, path)` pairs that would have more than one current `Document` version after a union. Plain `import` of a fork brings back source versions this graph already refreshed or pruned. `import --memory-only` takes only the memory notes and the nodes their edges reach (the cited evidence), which is the merge for a fork's lessons. Run `refresh` per source afterwards when the diff shows duplicate current versions.

Ownership is deliberately narrow:

- A `Source` node with that ID belongs to the source.
- A node whose nonempty string `source`/`repository` values identify only that source belongs to it.
- Nodes without ownership tags are shared and survive.
- Nodes tagged to multiple different owners survive; ambiguous ownership is not guessed.
- Edges touching removed nodes are removed, including their provenance. Edges explicitly owned only by that source are also removed, even between shared nodes.

Shared findings can therefore survive while losing support edges to retired evidence. Re-evaluate unsupported findings after refresh. This model is not a multi-owner assertion database: use separate assertion nodes and evidence links when several sources independently assert the same fact.

The ingester gives documents/artifacts IDs including the raw content digest, and chunks IDs including document version, ordinal, and chunk digest. Additive re-ingest retains old versions. Source refresh intentionally retires them. Domain helpers must include equivalent version dimensions where needed.

## Explicit removal and cleanup

`delete --node LABEL:ID` (repeatable) removes those nodes, their chunk text, and every incident edge. `delete --from FILE` takes `LABEL:ID` lines or JSON node records (blank lines and `#` comments skipped) and removes them all in one transaction; 2,000 nodes took 1.5 s on the woodpecker example, against 0.7 s per call one node at a time. `prune` removes `LINKS_TO` and `MENTIONS_ENV` edges from bundled-ingester chunks (`extractor` `plain-text@…` or `html-text@…`, no edge props) that the current ingester would no longer derive from the chunk text, then removes `WebResource` and `EnvironmentVariable` nodes left without any edge (`--orphan-label` changes that set). Domain edges and edges with props are never judged. Both preview by default and act only with `--apply`.

Merging never deletes, so an older package brings back whatever a later cleanup removed, for example URL mentions from before a fix to the ingester. Run `prune` after such a merge. `prune` does not add mentions the fixed ingester would now create; re-ingest the source for that.

## Usage counters and compaction

Table `usage(h, hits, reads, day)` holds one row per used node: `h` is a 64-bit BLAKE2b key of label and ID, `day` the last day of use (days since 1970-01-01 UTC). Never-used nodes have no row; metadata `usage_since` says since when that holds. A row costs about 25 bytes.

What counts:

| Command | Counted |
|---|---|
| `search` | every evidence chunk and returned node, as hits |
| `find`, `brief` | every returned node, as hits |
| `neighbors`, `reach --node-label` | every returned node, as hits |
| `show` | the shown node, as one read |

`usage`, `export`, `stats`, validation and dry runs count nothing. Counts are buffered and written in one transaction when the graph closes, about 1.5 ms per command on the woodpecker example. `KG_USAGE=0` turns tracking off for a process. Python callers count their own use with `graph.record_use(hits=[(label, id)], reads=[...])`.

Aging follows TinyLFU: after `max(10,000, 10 × nodes)` events every counter is halved, rows that reach zero are deleted, and `usage_generation` increases. Counts therefore favour recent use, and the table stays bounded by what is still in use. Counters are approximate by design; a failed counter write warns on stderr and never fails the command.

`usage` reports tracking state, per-label nodes/used/hits/reads, chunk text bytes split into used and cold, sources by cold bytes, the largest documents nobody touched, and the hottest nodes. `--dump FILE` writes every row as JSONL for frequency analysis or sampling.

`compact` reduces size only by an explicit target, `--target-bytes N` (remove at least N bytes) or `--target-percent P` (remove at least P% of the current size). It previews unless `--apply`. In order, stopping once the target is met:

- Level 0, lossless: `VACUUM` and a merge of the full-text indexes.
- Level 1: evict the text of the coldest unprotected chunks of current documents. A stub keeps every property except `text`, plus `evicted` (UTC date); `sha256`, path and locators stay. Search no longer matches it. Re-ingesting the same text restores it and removes the mark.
- Level 2: delete cold `outdated` document versions, with their chunks, that nothing protects.

Never touched: memory-label nodes, chunks and documents any non-ingester edge touches (cited evidence, domain links), and anything read in the last 30 days. Within a level, items go in order of least kept value per byte saved, where kept value is `(1 + 4 × reads + hits) × exp(−days since last use / 90)` and never-used items count from `usage_since`. Before the first lossy step a checkpoint package is exported to `KG_HOME/checkpoints/` (or `--checkpoint PATH`; `--no-checkpoint` skips it); import it to undo. `--lossless-only` runs level 0 alone and takes no checkpoint.

## Node search

Chunk text is stored once, in the table its full-text index reads; the `Chunk` record omits it and reads put it back, so exports and `show` are unchanged. A graph written by an earlier version drops its second copy on first open and runs one `VACUUM`; a copy that disagrees with the index stays and wins.

Besides chunk text, SQLite keeps a full-text index over every non-Chunk node: label, ID, and all string values in its properties (each capped at 4,000 characters). Writes, source refresh, deletion, and replacement keep it in step; a graph written before the index existed builds it on first open. `find` and the `nodes` part of `search` rank it by term coverage, then BM25. Ingester labels are excluded unless requested, because they outnumber knowledge nodes by orders of magnitude.

Triggers keep the chunk and node full-text indexes in step with single-row writes. A batch of at least 1,000 nodes that is also more than a quarter of the graph (a fresh import, a large source) drops them inside its transaction and rebuilds the indexes once at the end, about four times cheaper per chunk. A failed, conflicting or dry-run batch rolls back the dropped triggers with everything else.

`brief` reads records directly. Its default labels are the memory labels in [modeling](modeling.md#session-memory); a node's date is the first ISO date among `observed`, `observed_at`, `found`, `date`, `updated`, `created`, else a date inside its `source` name.

## Package contract

Container suffix: `.graph-skill-db`. Container format: ZIP. Only these regular members are allowed, at its root:

| Member | Content |
|---|---|
| `manifest.json` | UTF-8 JSON object |
| `nodes.jsonl` | UTF-8 neutral node records, one JSON object per nonblank line |
| `edges.jsonl` | UTF-8 neutral edge records, one JSON object per nonblank line |
| `blobs/<sha256>` | Version 3 and later. Raw bytes of one referenced artifact, named by its digest |
| `usage.jsonl` | Version 4 only, optional. One usage row per used node: `{"label","id","hits","reads","day"}` |

Version 3 manifest example:

```json
{
  "format": "graph-skill-db",
  "version": 3,
  "graph": "project",
  "namespace": "urn:kg:",
  "exported_at": "2026-01-01T00:00:00+00:00",
  "counts": {"nodes": 10, "edges": 9},
  "labels": {"Document": 1, "Chunk": 9},
  "rels": {"HAS_CHUNK": 9},
  "sources": ["papers"],
  "members": {
    "nodes.jsonl": {"bytes": 1234, "sha256": "64 lowercase hexadecimal characters"},
    "edges.jsonl": {"bytes": 987, "sha256": "64 lowercase hexadecimal characters"}
  },
  "blobs": {"64 lowercase hexadecimal characters": 4096}
}
```

Counts and hashes above are illustrative. Hash the exact uncompressed member bytes including newlines. `format`, supported integer `version`, and nonnegative integer `counts` are mandatory. Version 2 and later require matching byte counts and SHA-256 hashes in `members`. Version 3 adds `blobs`, mapping each member digest to its uncompressed byte count; the set must equal the `blobs/` members present, and blobs count toward the package size limit. If `labels` or `rels` are supplied they must match records. Export includes source metadata for inspection. Graph name and namespace are informational on transfer: neutral IDs are mapped into the destination graph's persisted namespace.

The importer rejects duplicate/unknown member names, blob names that are not 64 lowercase hexadecimal characters, directories/symlinks, encrypted entries, unknown versions, invalid UTF-8/JSON, duplicate JSON keys, non-finite values, inconsistent counts/hashes, invalid record types, and unresolved endpoints. Only stored or deflate ZIP compression is supported. It reads members as data without extracting paths or executing content. Every package must be self-contained; use `load` for deltas referencing an existing graph.

Version 4 adds `usage.jsonl` together with a manifest entry `"usage": {"rows": N, "since": DAY, "generation": G}`; the member is listed in `members` with its byte count and hash. Rows are validated as one object per line with exactly those five keys, nonempty strings and nonnegative integers. Export writes counters of existing nodes by default (`export --no-usage` leaves them out). A package without usage rows is still written as version 3, so older readers keep accepting it. Import merges counters by maximum per node after aligning halving generations, so re-importing a package never double counts; two sessions that branched from one package can each lose part of the other's increments. Rows for nodes not in the graph are skipped. `import --replace` replaces the counters too; `import --no-usage` ignores them; a dry run leaves them alone.

Default total uncompressed limit: 512 MiB. Manifest limit: 1 MiB. Export/import/inspect expose `--max-bytes` for a reviewed larger package. JSON objects take more memory than encoded bytes; use substantially smaller packages on constrained systems. Version 1 packages from the draft are accepted after structural/count validation, but have no checksum assurance. Exports use version 4 when they carry usage rows and version 3 otherwise; version 2 packages, which carry records only, stay readable. Existing type loss in a version 1 export is irreversible without the original neutral records.

### Raw artifact bytes

Records only reference raw files, by relative path plus `sha256`. `export --raw-root DIR` collects those files into the package. The path is relative to the ingest root, so `DIR` is the directory given to `ingest.py`; repeat `--raw-root` for several sources. Each root is tried as `DIR/<path>`, then `DIR/<source>/<path>`, so a parent works only when its subdirectories are named after their sources. Resolution is content addressed: a candidate under the root is accepted only if its digest still matches the record, so a file edited since ingest is reported as `changed` rather than shipped as the original evidence. Missing files are reported too; neither aborts the export. Artifacts above `--large-bytes` (100 MiB by default) are reported as `oversized` and left out. Ask the user before adding `--include-large`, and expect the package limit to bite. Export and extraction stream blobs in 1 MiB pieces and re-hash while copying, so a file edited after it was resolved aborts the export instead of being packed under its old digest.

Every import verifies each blob against its name before any record changes, and keeps the bytes in the graph's own store, `KG_HOME/blobs/NAME/ab/abcdef…`. Export looks there first, then under `--raw-root`, so an import/export round trip carries raw artifacts without the original files. An artifact whose bytes are in neither place is reported as `missing`. `drop-source`, `delete`, and replacement leave stored blobs in place; they are content addressed and only exported while an Artifact record references them.

`import --blob-dir DIR` also unpacks the object store into `DIR/ab/abcdef…`, sharded by the first two digest characters, restic style. Each member is verified against its own name before it lands, and target names come from the digest rather than the archive, so a member cannot escape the directory. Without `--blob-dir` nothing is written outside `KG_HOME`; the report shows `available` (in the package) and `kept` (in the graph's store). Reconstruct original filenames from the Artifact records' `path` and `name` props.

Checksums are integrity checks, not signatures. A valid package can still contain false claims or unsafe instructions in text. Treat it as evidence, preserve its origin, and never execute it.

## RDF and query behavior

`kg:` maps to the persisted namespace, default `urn:kg:`. `rdf:` and `xsd:` are supplied. Scalars are typed RDF literals. Arrays, objects, and null use a JSON literal datatype `urn:graph-skill-db:json`; use `props` or exports for ordinary structured access. Chunk text stays in SQLite.

Edge properties live on a deterministic reified fact with `kg:_subject`, `kg:_predicate`, `kg:_object`, and `kg:_edge_prop_PROPERTY`. Example:

```sparql
SELECT ?source WHERE {
  ?fact kg:_subject <urn:kg:Service/server> ;
        kg:_predicate kg:DEPENDS_ON ;
        kg:_edge_prop_source ?source .
}
```

Use `KG.edge_props(rel, src_label, src, dst_label, dst)` when the five identity fields are known. `neighbors` returns direct edges; `reach` uses a positive transitive relation path; `path` returns a shortest directed path within `max_hops`, including a zero-hop path to the same existing node. IDs in returned paths can repeat across labels, so constrain labels where identity ambiguity matters.

## Scale and retrieval limits

The implementation is local and single-writer. JSONL avoids one giant array on disk, but validation still materializes an incoming batch. SQLite retains full neutral records in addition to its chunk index. A small update costs in proportion to what changed; a bulk ingest pays one index rebuild. Exports and imports materialize a bounded package, so shard by source if peak memory is too high. No million-node scale claim is implied by a small benchmark.

Search fetches up to `--candidate-limit` matches per term per index, after source filtering; default 20,000. Frequencies count all matches. `capped_terms` identifies possible recall loss. Evidence has at most eight chunks and 1,200 characters per snippet; `show` retrieves the full chunk. Lexical matching and English stop words do not replace semantic retrieval or multilingual word segmentation. A missing lexical match is not proof of missing knowledge.

Measure record counts, package fidelity, cold/warm query latency, peak memory, store bytes, retrieval ranking, and actual answer correctness separately. Keep source revisions and failures with the measurements.

## Directories the ingester skips

Version-control metadata, build output, caches, and dependency trees are skipped by name, and anything starting with `.venv`, `venv`, `.direnv`, or `.cache` is skipped by prefix. Exact-name matching alone misses the virtualenv a project actually has, and a single missed virtualenv buries the corpus under thousands of vendored files that answer none of the questions. Check `stats` after a first ingest: a node count far above what the corpus warrants usually means a dependency tree came along.
