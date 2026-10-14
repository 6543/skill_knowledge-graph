# Modeling

## Start from questions

Write representative questions first. Add only labels and relations needed to answer them. A graph with many generic `MENTIONS` edges often performs worse than a small graph with explicit domain relations.

For each question, decide whether the answer needs:

- content retrieval;
- one-hop lookup;
- multi-hop traversal;
- absence or coverage analysis;
- aggregation;
- version or provenance comparison.

## Three layers

### Source layer

Use `Source`, `Repository`, `Document`, `Artifact`, and `Chunk` nodes. Record stable source names, relative paths, revisions, digests, and line ranges. This layer answers where evidence came from.

### Domain layer

Use domain nouns that match the material. Prefer stable IDs: fully qualified names, DOIs, schema paths, repository-relative symbol IDs.

| Material | Nodes that earn their place | Relations worth extracting |
|---|---|---|
| Research papers, preprints | `Paper`, `Author`, `Result`, `Method`, `Dataset` | `CITES`, `AUTHORED_BY`, `USES_METHOD`, `SUPERSEDES` |
| Encyclopedia or wiki articles | `Topic`, `Person`, `Organization`, `Event` | `LINKS_TO`, `PART_OF`, `DISCOVERED_BY` |
| Source code | `Module`, `Function`, `Type`, `Endpoint`, `Test` | `CALLS`, `IMPLEMENTS`, `DEPENDS_ON`, `TESTED_BY` |
| Infrastructure and configuration | `Service`, `Host`, `ConfigKey`, `Secret`, `Runbook` | `HAS_KEY`, `RUNS_ON`, `DEPENDS_ON`, `DOCUMENTED_BY` |
| Logs and incidents | `Incident`, `ErrorSignature`, `Component`, `Deploy` | `OBSERVED_IN`, `AFFECTS`, `CAUSED_BY`, `RESOLVED_BY` |
| Media-derived text | `Artifact` plus `Chunk` per page, slide, region, or timestamp | `HAS_CHUNK`, `DERIVED_FROM` |

The table is a starting point, not a schema to adopt wholesale. Add a label only when a question needs it.

### Evidence layer

Connect domain facts to their evidence. Put `source`, `path`, `line`, `extractor`, and `confidence` in edge or node properties when appropriate. Use explicit relations such as `DEFINED_IN`, `IMPLEMENTS`, `CONFIGURES`, `CALLS`, or `DOCUMENTS`.

## Nodes versus properties

Use a property for a scalar that is not independently traversed, such as a line number, digest, boolean, or display name.

Use a node when the value has identity, participates in several relations, has its own provenance, or must be counted and compared. An environment variable referenced by many documents should be a node. A chunk's start line should be a property.

## Identity

- IDs must be deterministic across rebuilds.
- Qualify ambiguous names. Prefer `repo:path:symbol` over `Create`.
- Include content digests in chunk IDs only when changed text should produce a new chunk identity.
- Do not use only a basename for a document ID.
- Separate version identity from concept identity when comparing revisions.

## Relation direction

Choose one readable direction and keep it consistent. `Service HAS_KEY ConfigKey` is easier to query than a mixture of `HAS_KEY` and `KEY_OF`. Add inverse edges only when measured queries justify the storage cost.

## Avoid false joins

Never connect records because unqualified display names happen to match. Resolve through a canonical ID, namespace, parser symbol, schema pointer, or explicit mapping. If resolution is uncertain, retain the mention as evidence and omit the semantic edge.

## Derived text

Text produced by OCR, transcription, image description, or table extraction is evidence of a different quality than text read off disk, and answers based on it should be able to say so. Keep the original file as an `Artifact`, attach the derived `Chunk` nodes to it, and put `extractor`, its version, and the page, slide, region, or timestamp on each chunk. When the same artifact is re-extracted with a better tool, a `--source` refresh or a merge then replaces the old derivation cleanly.

## Content chunks

Chunk Markdown at headings, code at structural boundaries when possible, and other text at bounded sizes. Preserve source-relative path plus start and end lines. Do not split only by raw character count when a parser can preserve a better unit.

File-scope search can combine evidence from multiple chunks, so chunks can remain locally coherent without preventing cross-section answers.
