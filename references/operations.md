# Operations

## Durable storage and recovery

`KG_HOME` contains `content/NAME.sqlite3` and `rdf/NAME.oxi`. SQLite is authoritative: neutral records, metadata, chunk bodies, and FTS indexes commit together with WAL and synchronous FULL. Oxigraph is a derived topology index. Never edit either store directly.

The code acquires Oxigraph's exclusive lock before opening SQLite. Use one `KG` instance per named graph, close it, and run CLI commands sequentially. Different graphs can be used independently. Python usage:

```python
from store import KG
with KG("project", create=True) as graph:
    graph.load(nodes, edges)  # additive, conflicts abort
    answer = graph.search_question("Where is the timeout configured?")
```

Malformed batches, missing endpoints, property collisions, and SQL transaction failures leave existing records and text unchanged. RDF is updated after the canonical transaction commits, and its completed revision is recorded only after `Store.flush()`. If the process fails during that update, records may already be committed and the index is left marked stale. Reopen or query the graph to retry before using topology. Retrying the same merge is safe. Persistent I/O errors require fixing the storage problem first.

The index is moved forward by the records a write actually touched, rather than being rebuilt whole every time. A rebuild is still used for a full replacement, for the first open of a graph whose index is missing or stale, and whenever the change covers a large enough share of the graph that the bulk path is cheaper. This is what keeps adding a single fact to a large graph a sub-second operation instead of one proportional to the whole corpus; both paths produce an identical index, which the tests assert directly.

This is recoverable consistency across two engines, not a distributed transaction. A process holding `graph.store` directly can bypass recovery; use the supported KG methods. Do not copy live database files as a backup. Use `export`, or close all writers and copy the complete graph directory including any WAL files. Package writes use a temporary file and atomic rename so an interrupted export does not truncate an existing package.

Older local draft stores migrate on first open by reconstructing neutral records and rebuilding chunk identity/indexes. Their previously discarded or stringified properties cannot be recovered automatically. Export and preserve the old graph before upgrading when the old tool is available. Legacy namespaces are inferred only from recognizable node/type IRIs; ambiguity fails rather than silently importing an empty graph. New stores persist their namespace. Explicitly reopening with a conflicting namespace fails.

A missing RDF directory can be rebuilt from the SQLite records. For a damaged RDF directory, stop writers, preserve a copy, then move that derived directory aside and reopen. For damaged SQLite, restore an exported package into a new graph name, or re-extract from the retained sources. Verify counts, evidence, and representative queries before using the restored graph.

## Merge and ownership

Default `load` and `import` merge. Properties omitted from an incoming node or edge remain unchanged. Equal values are idempotent, including nested JSON values. Conflicting fields abort the entire batch. `--on-conflict incoming` explicitly authorizes overwriting supplied conflicting fields; it retains unspecified fields and does not preserve overwritten history. Prefer version or assertion nodes when both values are meaningful.

`load --replace` replaces the whole graph. `load --source NAME` refreshes that source and rejects batches tagged to other sources. Edge endpoints may resolve against surviving external nodes. `drop-source --source NAME` retires it. These operations are explicit and destructive.

Ownership is deliberately narrow:

- A `Source` node with that ID belongs to the source.
- A node whose nonempty string `source`/`repository` values identify only that source belongs to it.
- Nodes without ownership tags are shared and survive.
- Nodes tagged to multiple different owners survive; ambiguous ownership is not guessed.
- Edges touching removed nodes are removed, including their provenance. Edges explicitly owned only by that source are also removed, even between shared nodes.

Shared findings can therefore survive while losing support edges to retired evidence. Re-evaluate unsupported findings after refresh. This model is not a multi-owner assertion database: use separate assertion nodes and evidence links when several sources independently assert the same fact.

The ingester gives documents/artifacts IDs including the raw content digest, and chunks IDs including document version, ordinal, and chunk digest. Additive re-ingest retains old versions. Source refresh intentionally retires them. Domain helpers must include equivalent version dimensions where needed.

## Package contract

Container suffix: `.graph-skill-db`. Container format: ZIP. Only these regular members are allowed, at its root:

| Member | Content |
|---|---|
| `manifest.json` | UTF-8 JSON object |
| `nodes.jsonl` | UTF-8 neutral node records, one JSON object per nonblank line |
| `edges.jsonl` | UTF-8 neutral edge records, one JSON object per nonblank line |
| `blobs/<sha256>` | Version 3 only. Raw bytes of one referenced artifact, named by its digest |

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

Default total uncompressed limit: 512 MiB. Manifest limit: 1 MiB. Export/import/inspect expose `--max-bytes` for a reviewed larger package. JSON objects take more memory than encoded bytes; use substantially smaller packages on constrained systems. Version 1 packages from the draft are accepted after structural/count validation, but have no checksum assurance. Exports always use version 3; version 2 packages, which carry records only, stay readable. Existing type loss in a version 1 export is irreversible without the original neutral records.

### Raw artifact bytes

Records only reference raw files, by relative path plus `sha256`. `export --raw-root DIR` collects those files into the package. Resolution is content addressed: a candidate under the root is accepted only if its digest still matches the record, so a file edited since ingest is reported as `changed` rather than shipped as the original evidence. Missing files are reported too; neither aborts the export. Artifacts above `--large-bytes` (100 MiB by default) are reported as `oversized` and left out. Ask the user before adding `--include-large`, and expect the package limit to bite. Export and extraction hold one blob in memory at a time, so a very large included artifact needs headroom.

`import --blob-dir DIR` unpacks the object store into `DIR/ab/abcdef…`, sharded by the first two digest characters, restic style. Each member is verified against its own name before it lands, and target names come from the digest rather than the archive, so a member cannot escape the directory. Without `--blob-dir` the records import and the blobs stay in the package; the count is reported as `available`. Reconstruct original filenames from the Artifact records' `path` and `name` props.

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
