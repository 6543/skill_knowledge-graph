# Modeling

The graph is not a copy of the sources. It is the smallest structure that answers the questions the sources are being kept for. Everything below is in service of that.

## Contents

- [Start from questions](#start-from-questions) - what shape of answer each question demands
- [The three layers](#the-three-layers) - source, domain, evidence
- [Node or property](#node-or-property) - the decision that most affects query power
- [Identity](#identity) - ID grammar, and why a bare name is a bug
- [Relations](#relations) - direction, naming, and cardinality
- [False joins](#false-joins) - the failure that quietly poisons a graph
- [Chunking](#chunking) - what a retrievable unit should be
- [Derived text](#derived-text) - OCR, transcripts, extracted tables
- [Domain starter models](#domain-starter-models) - papers, wiki, code, infrastructure, logs, media
- [Write domain helpers](#write-domain-helpers) - and reuse extraction skills that already exist
- [Growing a model](#growing-a-model) - changing a schema that already has data in it
- [Smells](#smells) - what a bad model looks like from the outside
- [Worked example](#worked-example) - one question set turned into a model

## Start from questions

Write ten to twenty representative questions before defining a single label. Then, for each one, decide what the answer actually requires. This is the whole design method: labels and relations that no question needs are cost without benefit, and a question with no structure behind it will fail no matter how good the search is.

| Question shape | Example | What the model must provide |
|---|---|---|
| Content retrieval | "What does the runbook say about connection exhaustion?" | Chunks with paths and line ranges. A domain layer may be unnecessary. |
| One-hop lookup | "Which service owns this config key?" | A relation between two domain nodes. |
| Multi-hop traversal | "Which services break if the scheduler goes down?" | A consistently directed relation, transitively walkable. |
| Absence or coverage | "Which endpoints have no test?" | Both sides modelled as nodes, so the missing side is queryable. |
| Aggregation | "Which module has the most callers?" | Relations as edges, not as text inside properties. |
| Provenance comparison | "Did this claim survive the 2025 revision?" | Version identity separate from concept identity. |

The last three are the ones that justify a graph at all. If every question is content retrieval, the source layer alone is enough and a domain layer is wasted effort.

A graph full of generic `MENTIONS` edges usually performs *worse* than a small graph with explicit relations, because every traversal drowns in near-meaningless hops. Reach for a specific relation or none at all.

## The three layers

### Source layer

`Source`, `Repository`, `Document`, `Artifact`, `Chunk`. Records where evidence came from: stable source names, relative paths, revisions, digests, line ranges. The bundled ingester builds this layer for text, and records anything it cannot read as an `Artifact`.

This layer answers "where did this come from" and should be complete before any domain modelling starts. Provenance not captured at ingest time cannot be reconstructed later.

### Domain layer

The nouns the questions are actually about: `Service`, `ConfigKey`, `Function`, `Paper`, `Incident`, `Author`. These carry stable IDs and few properties.

### Evidence layer

The edges that tie a domain fact back to the text that supports it, carrying `source`, `path`, `line`, `extractor`, and `confidence` where they apply. Use explicit relations: `DEFINED_IN`, `DOCUMENTED_BY`, `SUPPORTED_BY`, `OBSERVED_IN`.

The separation matters because the three layers have different lifetimes. Sources are re-ingested, domain facts are re-extracted, and analyst conclusions accumulate across sessions. A source refresh removes owned evidence and edges touching it. Shared conclusions remain, but may need new support. Use additive versioned records when retaining earlier evidence matters.

## Node or property

A **property** holds a scalar that is never traversed on its own: a line number, a digest, a boolean, a display name, a timestamp.

Make it a **node** when the value has identity of its own, participates in more than one relation, needs its own provenance, or has to be counted, grouped, or compared.

An environment variable referenced by twelve documents is a node: you will want to ask which documents mention it, and which service defines it. A chunk's start line is a property: nothing ever traverses to a line number.

The test that settles most cases: *could a question ever start from this value?* "Which files mention `DATABASE_URL`?" starts from the variable, so it is a node. Nobody asks "which chunks start at line 42", so that stays a property.

Getting this wrong in the node direction costs storage and clutters listings. Getting it wrong in the property direction makes a whole class of question unanswerable, and you will not notice until someone asks one. When genuinely unsure, prefer the node.

## Identity

IDs are the load-bearing part of the model. They must be **deterministic across rebuilds** - the same source material must always produce the same ID, or every re-ingest looks like a wholesale change and merges create duplicates instead of updating facts.

A useful ID grammar, in rough order of preference:

| Material | ID | Why |
|---|---|---|
| Code symbol | `repo:path/to/file.go:PackageName.Symbol` | Qualified by repository and file, so two `Create` functions never collide |
| Config key | `service/section.key` | Reads like the thing it names and sorts usefully |
| Document | `source:relative/path.md@<digest>` | Stable under re-ingest; never a bare basename |
| Chunk | `source:path@<document-digest>#chunk-000007-<digest16>` | Ordinal keeps ordering, digest makes edited text a new chunk |
| Paper | `doi:10.1145/3372297` or `arxiv:2401.01234v2` | Already a global identifier; do not invent one |
| Person | `orcid:0000-0002-1825-0097`, else `source:Family, Given` | Names are not identifiers; qualify by source when that is all you have |
| Incident | `INC-2026-05-03-webhook-panic` | Human-readable, sortable, unique |

Rules that follow from this:

- **Never use a bare display name as an ID.** `Create`, `config.yaml`, `Smith` are all guaranteed collisions the moment a second source arrives, and a collision between two unrelated corpora during a merge is silent data corruption.
- **Version evidence.** Include a document digest/revision and chunk digest in chunk identity. A changed passage becomes new evidence. An in-place correction requires an explicit conflict policy and should be backed up if its history matters.
- **Separate version identity from concept identity** when revisions are compared: `Paper/arxiv:2401.01234` for the work, `PaperVersion/arxiv:2401.01234v2` for the revision, joined by `HAS_VERSION`.
- **Qualify by source when merging is likely.** An ID that is only unique within one corpus becomes ambiguous in a shared graph, and `import` merges on ID.

## Relations

**Pick one direction and hold it.** `Service HAS_KEY ConfigKey` throughout is far easier to query than a mixture of `HAS_KEY` and `KEY_OF`, because every query has to enumerate whichever variants exist. Add inverse edges only when a measured query justifies the storage.

**Name the relation after what it asserts,** not after the extraction that found it. `CALLS` and `DEPENDS_ON` are claims about the world; `FOUND_BY_REGEX` is a note about tooling and belongs in an edge property.

**Put the qualifier on the edge, not in the name.** `CALLS` with `props: {"kind": "async"}` beats `CALLS_ASYNC` and `CALLS_SYNC`, because a question about calls in general no longer has to know every suffix.

**Model both sides of a coverage question.** "Which endpoints have no test?" is only answerable if `Endpoint` and `Test` are both nodes with a `TESTED_BY` relation between them. Absence questions are the ones a graph is uniquely good at, and they are also the ones a sloppy model quietly makes impossible.

## False joins

Never connect two records because unqualified display names happen to match. A `User` type in one repository and a `User` table in another are not the same thing, and one careless join turns a graph into confident nonsense that is very hard to detect afterwards, because the output still looks structured and cited.

Resolve through a canonical ID, a namespace, a parser symbol, a schema pointer, or an explicit mapping table. When resolution is genuinely uncertain, keep the mention as evidence and omit the semantic edge. A missing edge is a gap someone can find; a wrong edge is a lie the graph will repeat.

If a heuristic join is worth making anyway, record it honestly: put `confidence` and `extractor` on the edge so a later query can filter it out.

## Chunking

A chunk is the unit of retrieved evidence, so it should be the smallest span that still makes sense on its own.

- Markdown and prose: split at headings, then at a bounded size.
- Code: split at structural boundaries when a parser is available, otherwise at a bounded size.
- Config: keep a section together.
- Logs: split by time window or by event, not mid-record.
- Tables: keep the header with the rows.

Preserve the source-relative path and the start and end lines on every chunk. Search ranks a file by its best chunk and can combine evidence from several chunks of one file, so chunks can stay locally coherent without preventing cross-section answers.

Ranking also weighs path and heading matches heavily, which means **a good heading is retrieval infrastructure**, not decoration. When a helper emits chunks, give them real headings.

## Derived text

Text produced by OCR, transcription, image description, or table extraction is evidence of a different quality from text read off disk, and an answer resting on it should be able to say so.

Keep the original file as an `Artifact`. Attach derived `Chunk` nodes to it. On each chunk record `extractor`, its version, and the locator within the original: page, slide, region, or timestamp. When the same artifact is re-extracted with a better tool, give the extraction pass its own version identity. An additive merge preserves earlier evidence. An explicitly requested source refresh retires it.

An `Artifact` with no chunks is an honest statement that material is present but not yet readable. Never invent content from a filename, size, or media type.

## Domain starter models

Starting points, not schemas to adopt wholesale. Add a label only when a question needs it.

| Material | Nodes that tend to earn their place | Relations worth extracting |
|---|---|---|
| Research papers, preprints | `Paper`, `PaperVersion`, `Author`, `Claim`, `Method`, `Dataset` | `CITES`, `AUTHORED_BY`, `USES_METHOD`, `EVALUATED_ON`, `SUPERSEDES`, `CONTRADICTS` |
| Encyclopedia and wiki articles | `Topic`, `Person`, `Organization`, `Event`, `Place` | `LINKS_TO`, `PART_OF`, `DISCOVERED_BY`, `OCCURRED_AT` |
| Source code | `Module`, `Function`, `Type`, `Endpoint`, `Test`, `Dependency` | `CALLS`, `IMPLEMENTS`, `DEPENDS_ON`, `TESTED_BY`, `EXPOSES` |
| Infrastructure and config | `Service`, `Host`, `ConfigKey`, `Secret`, `Runbook`, `Alert` | `HAS_KEY`, `RUNS_ON`, `DEPENDS_ON`, `DOCUMENTED_BY`, `ALERTS_ON` |
| Logs and incidents | `Incident`, `ErrorSignature`, `Component`, `Deploy`, `Runbook` | `OBSERVED_IN`, `AFFECTS`, `CAUSED_BY`, `RESOLVED_BY`, `FOLLOWED` |
| Legal and policy | `Clause`, `Obligation`, `Party`, `Jurisdiction` | `BINDS`, `AMENDS`, `APPLIES_IN`, `REFERENCES` |
| Media-derived text | `Artifact` plus a `Chunk` per page, slide, region, or timestamp | `HAS_CHUNK`, `DERIVED_FROM` |

Mixed corpora are normal and are where a graph pays off most: an `Incident` node linking a log `ErrorSignature` to the `Function` that raised it and the `Runbook` that resolves it answers a question no single source could.

## Write domain helpers

The bundled ingester is deliberately generic. It builds the source layer and stops. **Writing a small domain-specific helper alongside it is the expected way to use this skill, not a workaround.** The domain layer is where a graph gets its value, and nobody can ship a universal extractor for it.

A helper is worth writing whenever a question needs structure the generic ingester cannot see: a call graph, a citation network, a config schema, an incident timeline, an ownership map.

What makes a good one:

- **It emits the same neutral records** as `ingest.py`, so `validate`, `load`, `export`, and `import` all work unchanged. That is the entire integration contract.
- **It is deterministic.** Run it twice on unchanged input and the records must be byte-identical, or every rebuild looks like a change and merges duplicate facts.
- **It uses a real parser** where one exists. A language's own AST, a YAML or TOML parser, a schema library, or an existing code-indexing capability all beat regular expressions, which quietly produce false joins on the cases you did not think of.

  For source code this is worth spelling out. A structural indexer such as `codebase-index`, if it is installed, yields declarations with their locations and call edges with a confidence label, which map straight onto `Symbol` nodes and `CALLS` edges carrying `extractor`, `line`, and `confidence`. Shell out to whatever is present and skip the layer cleanly when nothing is: this skill never requires it, and a graph with no `Symbol` layer is still a working graph. Drop an edge whose target name is ambiguous rather than guessing, since that is precisely the false join described above.
- **It records its own provenance:** `extractor` name and version on the facts it creates, so a later pass can tell which tool made which claim and supersede it cleanly.
- **It stays outside this skill.** Keep helpers with the corpus or the project they serve. They track a domain that changes on its own schedule, and freezing them into a general skill is how they rot.
- **It leaves the text alone.** Helpers add relations. Chunk bodies come from the ingester, or from a documented extraction pass.

Start narrow: one relation that answers one failing question, validated and loaded with `--merge`, beats a speculative extractor that produces ten relations nobody queries.

## Reuse extraction that already exists

Before writing anything that reads an unfamiliar format, **check the available skills and tools for one that already handles it**. PDF, document, spreadsheet, image, transcription, code-indexing, and data-analysis capabilities are commonly installed, they are maintained by someone else, and they handle edge cases a hand-rolled parser will not. Using a matching one is always the better first move.

If a capability exists but is a near miss, use it for what it does well and add a thin helper for the remainder rather than replacing it.

If nothing fits, **ask the person what the material contains and how they want it extracted** before building anything: full text or layout-preserving text, tables as tables or as prose, captions, per-slide notes, frame descriptions, OCR language. They usually know which parts matter, and a wrong extraction choice is expensive to discover after the graph is built and queried. Then write the extractor for that specific job, feed the output back as `Chunk` nodes with the provenance described under [Derived text](#derived-text), and treat the code as throwaway tooling.

Never hardcode another skill's installation path. Discover what is available at the time you need it.

## Growing a model

Schemas change once a graph has data in it. Three moves cover almost everything:

- **Adding a label or relation** is safe. Extract it, load it with `--merge`, and existing facts are untouched.
- **Refreshing one corpus** is the explicit destructive `load --source NAME`. It removes owned nodes and affected edges, potentially leaving shared findings without support. Prefer additive versioned records unless retirement is requested.
- **Renaming a label or changing an ID scheme** is a rebuild, not a migration. IDs are the identity of every fact that references them; changing them in place produces duplicates that are hard to find later. Create explicitly versioned replacement records and update their links, or rebuild a new named graph. A full `--replace` load needs authorization to discard existing knowledge.

Keep the neutral JSONL inputs and portable exports. SQLite stores the authoritative records; RDF is derived. Recovery details are in operations.

Before importing, inspect source metadata and identity conventions. Conflicting property values abort the merge by default, but unrelated entities with identical IDs and disjoint properties could still fuse. Qualify IDs and model explicit mappings; source-name checks alone cannot prove identity.

## Smells

- Most edges are `MENTIONS` or `RELATED_TO`. The model is not carrying any real claims.
- The same concept appears under several labels because different extractors named it differently.
- IDs contain absolute paths, timestamps, or process IDs, so nothing is stable across rebuilds.
- A relation exists in both directions with no query that needs either.
- Chunk text is duplicated onto domain nodes, so the graph is large and the text index is redundant.
- Every question is answered by search alone and no traversal is ever used. The domain layer is not paying for itself.
- Absence questions cannot be asked because one side was modelled as a property.

## Worked example

Questions, for a mixed infrastructure and log corpus:

1. Which config key controls the database pool size, and where is it documented?
2. Which services depend on PostgreSQL?
3. Which error signature appeared during the 3 May incident, and which runbook covers it?
4. Which services have no runbook?

Question 1 needs only the source layer plus one node: `ConfigKey` with a `DOCUMENTED_BY` edge to a `Chunk`. Question 2 needs `Service` and `DEPENDS_ON`. Question 3 needs `Incident`, `ErrorSignature`, `Runbook`, and the edges `OBSERVED_IN`, `AFFECTS`, `RESOLVED_BY`. Question 4 needs `Runbook` as a node so its absence is queryable - a `runbook: "..."` property on `Service` would make it unanswerable.

That is the whole model. Five domain labels, six relations, everything else left in the source layer:

```json
{"label":"Service","id":"gitea","props":{"name":"Gitea"}}
{"label":"ConfigKey","id":"gitea/database.MAX_OPEN_CONNS","props":{"section":"database","name":"MAX_OPEN_CONNS"}}
{"label":"Incident","id":"INC-2026-05-03-webhook-panic","props":{"opened":"2026-05-03","severity":"high"}}
{"rel":"HAS_KEY","src_label":"Service","src":"gitea","dst_label":"ConfigKey","dst":"gitea/database.MAX_OPEN_CONNS"}
{"rel":"DOCUMENTED_BY","src_label":"ConfigKey","src":"gitea/database.MAX_OPEN_CONNS","dst_label":"Chunk","dst":"ops:runbooks/incident-db-connection-exhaustion.md#chunk-000002-1a2b3c4d5e6f7890","props":{"extractor":"ini-keys@1","line":24}}
```

Question 4 then falls out of the structure:

```sparql
SELECT ?id WHERE {
  ?s a kg:Service ; kg:id ?id .
  FILTER NOT EXISTS { ?s kg:DOCUMENTED_BY ?r . ?r a kg:Runbook }
}
```

Note what is *not* in the model: no `Author`, no `Deploy`, no `Alert`. They belong to questions nobody asked. Add them when someone does.
