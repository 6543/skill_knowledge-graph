# Operations

## Storage layout

With `KG_HOME=.kg`, topology lives at `.kg/rdf/NAME.oxi` and searchable content at `.kg/content/NAME.sqlite3`. Keep the two in separate directories and let the code open SQLite before Oxigraph; that ordering avoids storage lifecycle interference observed with some embedded-store combinations.

Expect the derived stores to be several times the size of the source text: on a mixed prose and code corpus of roughly 11 MB, the RDF store came to about 31 MB and the content store about 62 MB. The exported `.graph-skill-db` package for the same graph was 9 MB, because it holds compressed neutral records rather than indexes. Budget for the indexes, ship the package.

## Load modes

`load` validates everything before it writes, so malformed input leaves the current generation untouched.

- **Default replace** clears the graph. Use it when the graph has one source, or when every source is being rebuilt together. Material deleted upstream must stop being findable.
- **`--source NAME`** clears only the nodes, chunks, and edges that source contributed, then loads the new records. This is the right refresh for a multi-source graph: rebuilding one corpus should never discard the others.
- **`--merge`** upserts node properties and adds relations, removing nothing. Edge endpoints may point at nodes already in the store, which is what lets a working session attach findings to existing evidence. Because a merge produces no deletion event, it is the wrong mode for a source refresh.

Shared nodes such as environment variables or web resources are not owned by a single source, so dropping a source can leave them without edges. They are harmless; if they clutter a listing, filter by label in the query rather than deleting them.

## Packages

`export` rebuilds the neutral records from both stores and zips them with a manifest. `import` merges them back. The round trip is exact: export, import into an empty graph, re-export, and the records are identical.

Read the manifest with `inspect` before importing into a populated graph. It carries the source names, label and relation counts, and the export timestamp - enough to see whether an incoming package overlaps with something already loaded. Overlapping IDs are merged, with the incoming properties winning, so a collision between two unrelated corpora that both used bare names as IDs is silent data corruption. Qualify IDs at extraction time to prevent it.

Keep the neutral inputs for anything that matters. The two stores are derived indexes; the records are the source of truth.

## Large corpora

- Use JSONL to avoid one huge JSON array on disk.
- Split extraction by stable source, load each with `--source`, and keep per-source record files so any one corpus can be rebuilt alone.
- Keep bodies in `Chunk.text` only; never duplicate them onto domain nodes.
- Exclude generated output, dependency caches, virtual environments, and repository metadata unless the questions need them.
- Record oversized and unreadable files as `Artifact` nodes, then add extracted derivatives separately so the two kinds of text stay distinguishable.
- Measure cold open, warm open, median and p95 query latency, peak RSS, store bytes, and retrieval accuracy. Cold open is dominated by page cache, so a first-run figure says more about the machine than the graph; report warm numbers alongside it.

The validator holds a batch in memory. For multi-million-node graphs, validate sorted shards independently and add a disk-backed endpoint check before loading. Do not claim unbounded scale from a small benchmark.

## Query order

1. Search for likely evidence, and read the `unmatched` terms before concluding anything is absent.
2. Use `show` and `neighbors` for local graph context.
3. Use SPARQL for joins, counts, exclusions, and transitive relations.
4. Read the original text before answering.

The CLI supplies the `kg:`, `rdf:`, and `xsd:` prefixes. Node classes and properties live in the `kg:` namespace; `kg:id` carries the node ID.

## Recovery

If an index is damaged, stop querying it, keep the neutral inputs and source revisions, pick a clean graph name, and do a full replacement load. Verify counts and rerun the benchmark before pointing anything at the rebuilt graph. If the neutral inputs are gone but the graph still opens, `dump` or `export` it first - that recovers the records from the indexes.
