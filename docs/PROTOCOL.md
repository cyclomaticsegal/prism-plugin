# PRISM — Architecture Protocol

> How the system is laid out, where state lives, and where the boundaries between code and judgment sit. Behavioural guidance for Claude lives in the skills (`prism-bootstrap`, `prism-starter`, `prism-companion`), not here. This document describes the system; the skills tell Claude how to act.
>
> Note on naming: PRISM was developed under the working title "Frameworks of Understanding." References to "the brain" mean the user's PRISM instance.

**Last updated:** 2026-04-25
**Version:** 5.0 (post-B remediation)

---

## System Architecture

PRISM is a personal knowledge base with a knowledge graph, hybrid search, and optional prediction capabilities. `prism-brain.db` is the single source of truth for all state.

```
workspace/                                <- The user-chosen folder.
└── prism/                               <- Single container (everything below).
    ├── prism-brain.db                    <- SQLite database. Single source of truth.
    ├── prism-inbox/                      <- Drop zone for new material (files;
    │                                        subfolder names act as classification hints)
    ├── prism-sources/                    <- Read-only archive of ingested files
    ├── prism-extensions/                 <- Optional drop-in integrations
    │   └── folds/                        <- 51Folds prediction integration (id "folds")
    ├── prism-axioms.md                   <- Read-only PROJECTION of the axioms table
    │                                        (regenerated on every revision; do not edit)
    ├── prism-graph.json                  <- Read-only PROJECTION of nodes/edges
    └── prism-graph-explorer.html         <- Read-only PROJECTION (interactive D3.js map)
```

### Layer Summary

| Layer | Purpose | Authoritative? |
|---|---|---|
| `prism-brain.db` | Graph, chunks, embeddings, sources, predictions, **domains**, **axioms** | Yes. Single source of truth. |
| `engine/brain.py` | Engine: deterministic operations against the database | Code, not judgment. |
| `server/index.js` (MCP) | Translates Cowork tool calls into engine operations | Thin wrapper. No logic. |
| `prism/prism-inbox/` (files in) | Raw material the user is adding | Input only. Files move to `prism/prism-sources/` after ingestion. |
| `prism/prism-sources/` (files in) | Archived copies of ingested material | Reference only. The DB owns the content. |
| `prism-axioms.md` | Read-only projection of the `axioms` table | No. Projection. Edits are overwritten. |
| `prism-graph-explorer.html` | Read-only projection of the graph | No. Projection. Regenerated on mutation. |
| `prism-graph.json` | Read-only projection of nodes/edges | No. Projection. |

**Key distinction.** Authoritative state is the database. Inputs (files, pasted URLs/text) flow in once. Projections (HTML, JSON, prism-axioms.md) regenerate from the database. If a projection and the database disagree, the database wins; rebuild the projection.

---

## What Is Deterministic and What Is Not

The engine (`brain.py`) is deterministic. Same input, same database, same results. Chunking, embedding, search ranking, classification scoring, graph mutations, projection regeneration all produce identical output every time.

**Claude's role is judgment, not mechanics.** When to call `prism_core_search`, how to phrase a challenge to an axiom, when to surface a structural observation — these involve language generation and judgment. The skills (not this protocol) tell Claude how to make those calls.

**Rule:** everything that can be code should be code. If a behaviour can be a threshold check, a database trigger, or a post-write hook, it does not belong in a natural-language instruction.

---

## prism-brain.db — Single Source of Truth

All system state lives in the SQLite database.

### Tables

| Table | Purpose |
|---|---|
| `nodes` | Graph nodes: domains, concepts, sources, predictions |
| `edges` | Graph relationships (typed, labelled) |
| `chunks` | Document text chunks for search (with content hashes for dedup) |
| `chunks_fts` | FTS5 virtual table for BM25 keyword search |
| `embeddings` | Vectors per chunk (sentence-transformers or TF-IDF) |
| `domains` | Domain definitions (label, short_label, color, keywords). Authoritative. |
| `axioms` | User's axioms with revision history (`superseded_by`, `superseded_at`). Authoritative — `prism-axioms.md` is a regenerated projection. |
| `predictions` | Prediction model registry. Owned by core; CRUD called by the 51Folds extension via the restricted brain API. |
| `meta` | Key-value store: config, stats snapshots, timestamps |

### Source registry

The source registry is a database query, not a file. The MCP server exposes it via `prism_core_index`. No `INDEX.md` to maintain.

---

## Session Startup

The Cowork VM resets between sessions. The MCP server handles startup automatically:

1. `bootstrap.sh` installs Python dependencies (scikit-learn, numpy, sentence-transformers). Idempotent.
2. `brain.py` auto-restores `prism-brain.db` from the workspace into the session VM.
3. The MCP server starts, registers all core tools (see `manifest.json`), and discovers any extensions in `prism/prism-extensions/`.
4. The `prism-bootstrap` skill activates and initialises the workspace if needed.

The brain is immediately searchable. No manual steps.

---

## Ingestion

The user requests ingestion explicitly. The MCP server does **not** watch the filesystem and does **not** auto-trigger.

Three entry points (any combination):

- `prism_core_ingest` — processes files in `prism/prism-inbox/`. Subfolder names act as classification hints.
- `prism_core_ingest_url` — fetches a URL, extracts text, ingests as a new source.
- `prism_core_ingest_text` — ingests pasted text as a new source.

In all three, the engine chunks, embeds, classifies, and writes the source transactionally. Low-confidence classifications land as-is — ingestion is never blocked by Q&A. Post-hoc clarification belongs in the Graph Explorer (cluster labelling) or in `prism-companion` conversational observations.

Files move from `prism/prism-inbox/` to `prism/prism-sources/` after processing. `prism/prism-sources/` is a read-only archive; the database owns the content. Editing or deleting a file in `prism/prism-sources/` does **not** affect the brain. `prism_core_reingest <source_id>` re-reads the file. `prism_core_source_delete <source_id>` removes the source from the database (file untouched).

---

## Source Lifecycle

| Action | Tool | Effect on database | Effect on `prism/prism-sources/` file |
|---|---|---|---|
| Ingest from inbox | `prism_core_ingest` | Source row + chunks + embeddings + edges | File moved into `prism/prism-sources/` |
| Ingest URL | `prism_core_ingest_url` | Same | New file written to `prism/prism-sources/` |
| Ingest pasted text | `prism_core_ingest_text` | Same | New file written to `prism/prism-sources/` |
| Re-read after edit | `prism_core_reingest` | Chunks + embeddings regenerated; node and graph wiring preserved | None |
| Remove from brain | `prism_core_source_delete` | Source row, chunks, embeddings, edges removed | None — file remains |
| Edit a file in `prism/prism-sources/` | (no tool) | None | File changes; brain stays stale until `prism_core_reingest` |
| Delete a file from `prism/prism-sources/` | (no tool) | None | File gone; brain still has the chunks |

The boundary is unambiguous: the database is the truth, files are reference material.

---

## Searching

The brain is searched when Claude judges that a question would benefit from the user's reading. There is no auto-fire on every prompt — MCP cannot intercept prompts. The decision logic lives in `prism-bootstrap` (retrieval section).

When invoked, the engine runs hybrid search (BM25 keyword + cosine similarity, blended via Reciprocal Rank Fusion) and returns the top-k results as structured tool data. Claude synthesises and cites source IDs.

---

## Domains

Domains live in `prism-brain.db.domains`. There is no fixed list and no hardcoded defaults — the table starts empty and populates through:

- Subfolder names in `prism/prism-inbox/` (seeded automatically on ingestion).
- Prompt context the user supplies during an ingest call (honoured as direction).
- The TF-IDF classifier comparing source text against existing domain keyword descriptions.
- Conversational refinement: `prism_core_domains_set` writes through `prism-companion` Q&A and Graph Explorer cluster labelling.

`domains.json` is no longer read at all. A workspace with a leftover file from an older install is ignored — migrate by calling `prism_core_domains_set` for each row, then delete the file.

---

## Axioms

Axioms live in `prism-brain.db.axioms`. The user does not hand-edit `prism-axioms.md` — it is a regenerated read-only projection.

Authorship pattern: Claude drafts an axiom from conversation; the user accepts, sharpens, or rejects; Claude calls `prism_core_axioms_revise`. Each revision marks the predecessor `superseded` and stores the new active row. `prism_core_axioms_history` returns the full revision chain.

Coaching discipline (citation tests, boundary requirement, platitude pushback) lives in `prism-companion`.

---

## Graph Explorer

`prism-graph-explorer.html` is a static D3.js projection regenerated from `prism-brain.db` after every mutation.

Two interaction patterns:

- **Concept / source nodes**: the detail panel offers "Summarise" and "Ask a question" buttons. Both `postMessage` a structured action up to Cowork.
- **Domain nodes**: the detail panel offers "Refine label / keywords" and "Review cluster members" buttons. Both `postMessage` AND copy a Cowork-ready prompt to the clipboard. The user pastes the prompt into Cowork; Claude calls `prism_core_domains_set` (or `prism_core_tag`) to apply changes.

The explorer never writes to `prism-brain.db` directly — it generates Cowork prompts, Claude does the writes. This keeps the explorer fully static (file://) while still supporting editing.

---

## Node and Edge Types

### Nodes

| Type | ID Pattern | Description |
|---|---|---|
| domain | D1, D2, ... | Top-level thematic category |
| concept | C01, C02, ... | A discrete intellectual claim or framework |
| source | S01, S02, ... | A source document |
| prediction | P01, P02, ... | A probabilistic model result (extension) |

### Edges

Edges have free-form `type` and `label` text fields. The system does not enforce a fixed set. Common types:

| Type | Meaning |
|---|---|
| contains | Domain includes this concept |
| sourced_from | Source provides evidence for concept |
| relates_to | General thematic connection |
| exemplifies | Instance of a broader concept |
| enables / causes / requires / drives / critiques / explains | Specific causal or structural relations |
| quantifies | Prediction assigns probability to a concept |

New types do not require schema changes. They are labels.

---

## Design Principles

1. **`prism-brain.db` is the single source of truth.** Inputs (files, URLs, text) flow in once. Projections (HTML, JSON, `prism-axioms.md`) rebuild from the database. If they disagree, the database wins.
2. **The graph is for discovery.** Domains show density; cross-domain edges show synthesis; absence of bridges is itself a signal.
3. **Concepts are atomic.** Not paragraphs, not documents — discrete nameable claims that relate to other concepts.
4. **Source files are evidence, not structure.** The structure lives in the graph.
5. **Cross-domain edges are the most valuable part of the graph.** Bridges are where original thinking lives.
6. **Everything deterministic lives in code.** Thresholds, scoring, classification, projection regeneration. Not in instructions Claude interprets.
7. **The skills govern judgment, not mechanics.** When to suggest corrections, how to phrase challenges, when to surface observations — `prism-bootstrap`, `prism-starter`, `prism-companion`. Not this document.
8. **Automatic where the system can be sure.** Database mutations regenerate projections. Ingestion runs end-to-end without intermediate prompts. What requires judgment (whether to search, when to label a cluster) stays a judgment call.
9. **Grow incrementally.** Add sources regularly. Refine axioms when new material challenges them.
10. **Name precisely.** Use source terminology, not paraphrases.
11. **Extensions are optional.** The core engine works without any extensions. Extensions add capability without coupling.

---

## Settled Architectural Decisions

| Decision | Resolution | Rationale |
|---|---|---|
| Concept extraction during ingestion | Aggressive with deferred review | Manual approval per concept does not scale during setup. Review happens later — opt-in via Graph Explorer or `prism-companion` conversation. |
| Brain search invocation | Claude judges per prompt; no auto-fire | MCP cannot intercept prompts. Decision rules live in `prism-bootstrap`. |
| Cluster labelling Q&A | Lives in Graph Explorer + `prism-companion`, never in ingestion path | The user explicitly opts in by clicking a cluster or talking with Claude. Ingestion stays frictionless. |
| Domain inference | No hardcoded list. Folder hints + prompt context + classifier; refined conversationally. | "10 generic domains" was Simon-specific and contrary to the methodology. |
| Domains and axioms storage | `prism-brain.db` tables, not files | Files existed for hand-editing; if Claude is the scribe, the file rationale evaporates. `prism-axioms.md` is a regenerated projection. |
| Predictions location | Table in core, tooling in 51Folds extension | A prediction is a node with metadata (graph concern); orchestration is API-specific (extension concern). |
| Extension API surface | Restricted: `search`, `addNode`, `addEdge`, `getGraphData`, `exportGraph`, `stats`, `predictionSave/Get/Update/List/IngestNarrative`, `ingestText` | Prevents coupling. Expanded as needed. |
| Storage engine | SQLite for v1; cloud Postgres migration path exists behind MCP abstraction | Personal use does not need concurrent access. If you outgrow a local file, you outgrow local infrastructure. |

---

## Extensions

Extensions live in `prism/prism-extensions/` as self-contained folders. Each declares what it provides and what it needs in `manifest.json`. The MCP server discovers extensions at startup.

- Install: drop the folder.
- Remove: delete the folder.
- Update: replace the folder.

The core engine never depends on extensions. If an extension is missing, the core works exactly as before.

Extension-specific protocols (51Folds prediction pipeline, dud detection, credit management) live inside the extension's own documentation, not here.

---

## Framework Purity

The brain contains only user-curated content. Admissible inputs:

1. Files dropped into `prism/prism-inbox/` or pasted as URLs / text into the prompt.
2. Concepts Claude extracts from admissible sources during the ingestion pipeline (proposed for review).

Not admissible: Claude's training knowledge, web search results, prior session memory, cross-skill context. These may inform Claude's reasoning but do not become brain content.

---

## Session Continuity

**Persists between sessions:**
- All workspace files (`prism-brain.db`, `prism/prism-inbox/`, `prism/prism-sources/`, `prism/prism-extensions/`)
- Projections (`prism-graph-explorer.html`, `prism-graph.json`, `prism-axioms.md`) — but they can always be regenerated from `prism-brain.db`.

**Rebuilt each session:**
- Python dependencies (via `bootstrap.sh`)
- Session-dir working copy of `prism-brain.db` (auto-restored from workspace)
- MCP server connection and extension discovery

**At session start, Claude should know:**
- `prism-brain.db` auto-restores. Searches work immediately.
- Skills (`prism-bootstrap`, `prism-starter`, `prism-companion`) carry behavioural guidance — this protocol describes only architecture.
- Projections regenerate from the database. If something looks wrong in `prism-graph-explorer.html`, rebuild rather than diagnose.

---

*Versioned alongside the system. Update when the architecture changes; behavioural changes go in the skills.*
