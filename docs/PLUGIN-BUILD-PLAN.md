# PRISM Plug-in — Build & Remediation Plan

**Purpose.** Authoritative inventory of every artefact in the PRISM plug-in, the repository structure, and the build state after the v1.0 → v1.0-remediated remediation. This document is the spec a coding agent should follow if asked to extend or rebuild the plug-in.

**Note on naming.** PRISM is the product name. It was extracted from a working personal system ("Frameworks of Understanding"). The architecture, engine, and methodology are the same — the rename reflects the transition from a personal tool to a generic, distributable plug-in. References to "the brain" mean the user's PRISM instance.

**Last updated:** 2026-04-25 (post-remediation)
**Version:** 2.0 (replaces the v1.0 build plan that described the pre-remediation design)

---

## Repository Structure

```
prism-plugin/
│
├── README.md                         # User-facing readme (install, multi-modal ingestion)
├── LICENSE                           # MIT
├── .gitignore                        # node_modules, __pycache__, *.db, .env, runtime workspace files
├── manifest.json                     # Plug-in manifest (name, version, entry point, skills, 22 core tools)
│
├── server/                           # MCP Server
│   ├── index.js                      # Registers core tools, discovers extensions, exposes the restricted brain API
│   ├── package.json                  # Node deps (@modelcontextprotocol/sdk, zod)
│   └── package-lock.json
│
├── engine/                           # Core engine
│   ├── brain.py                      # Engine: ingest, search, graph, classify, projection regen, domains/axioms CRUD
│   ├── bridge.py                     # JSON-over-stdio bridge between MCP server (Node) and brain.py (Python)
│   ├── bootstrap.sh                  # Session startup: install Python deps
│   └── requirements.txt              # Python deps (numpy, scikit-learn, sentence-transformers, optional URL deps)
│
├── templates/                        # Runtime template (only the explorer template ships now)
│   └── graph-explorer-template.html  # D3.js explorer shell, populated from prism-brain.db on every mutation
│
├── skills/                           # Claude skills (UTF-8 markdown, never binary blobs)
│   ├── prism-bootstrap/SKILL.md      # Session initialisation: verify engine, retrieval guidance, ingestion dispatch, citation rules
│   ├── prism-starter/SKILL.md        # New-user onboarding (ingest-first, no blocking Q&A)
│   └── prism-companion/SKILL.md      # Ongoing coaching: citation tests, boundary discipline, confidence review, concept QA, retrieval tree
│
├── reference-brain/                  # Demo brain shipped with the plug-in
│   ├── brain-reference.db            # Pre-built (built by reference-brain/build.py)
│   ├── AXIOMS-reference.md           # Example axioms (reference content, not a projection)
│   ├── domains-reference.json        # Reference domain configuration
│   ├── GRAPH-reference.json          # Pre-built graph data (reference)
│   ├── build.py                      # Builds the reference brain via the new DB-backed APIs
│   └── sources/                      # 32 public-domain texts (Smith, Bacon, Kant, Locke, ...)
│
├── extensions/                       # Drop-in extensions
│   └── 51folds/                      # Lean v1 51Folds API integration
│       ├── client.js                 # HTTP wrappers for api.51folds.ai and app.51folds.ai
│       ├── tools.js                  # 4 tools: prism_folds_refine_thesis, prism_folds_create, prism_folds_status, prism_folds_ingest_results
│       ├── manifest.json             # Extension manifest (tools, node_types, edge_types, env requirements)
│       └── .env.example              # Bearer-token template (.env is gitignored)
│
├── docs/                             # Design documents (developer-facing)
│   ├── PLUGIN-BUILD-PLAN.md          # This file
│   ├── PLUGIN-HOW-IT-WORKS.md        # Architecture and component reference (post-remediation)
│   ├── PLUGIN-NEW-USER-EXPERIENCE.md # User journey (ingest-first)
│   └── PROTOCOL.md                   # Architecture protocol (v5.0; behavioural guidance lives in skills, not here)
│
├── scripts/
│   └── package.sh                    # Builds dist/prism-<version>.tar.gz; UTF-8 guard on every staged SKILL.md
│
└── tests/                            # Pytest suite (engine, classifier, bootstrap, extensions, daemon, axioms lifecycle, source lifecycle, ingestion modes, packaging guard)
    ├── test_engine.py                # Engine: ingest, search, graph, tag, stats, dedup, rollback
    ├── test_classifier.py            # Classifier: domain matching, multi-domain, confidence, fallback chain
    ├── test_bootstrap.py             # First-run + idempotency (DB-backed; no template copies)
    ├── test_extensions.py            # Extension discovery + restricted API
    ├── test_extraction.py            # Concept proposals lifecycle
    ├── test_reference.py             # Reference brain artefacts and search
    ├── test_server.py                # Bridge commands and subprocess mode
    └── fixtures/                     # 5 sample sources + domains-test.json (seeded into the domains table by tests; not read as a config file at runtime)
```

---

## Authoritative State (post-remediation)

### Single source of truth

`prism-brain.db` (SQLite) holds **all** state. Everything else is either an input (files in `prism/prism-inbox/`, content pasted via the prompt) or a projection (HTML, JSON, `prism-axioms.md`).

### prism-brain.db tables

| Table | Purpose |
|---|---|
| `nodes` | Graph nodes (domain, concept, source, prediction) |
| `edges` | Typed graph relations |
| `chunks` + `chunks_fts` | Document chunks (BM25 via FTS5) |
| `embeddings` | One vector per chunk |
| `domains` | **Authoritative domain configuration** — replaces the deleted `domains.json` |
| `axioms` | **Authoritative axioms** with revision history (`superseded_by`, `superseded_at`) — `prism-axioms.md` is a regenerated read-only projection |
| `predictions` | Prediction registry (table in core; tooling lives in the 51Folds extension) |
| `meta` | Key-value: config, stats snapshots, timestamps |

### Workspace artefacts created at first launch

| Artefact | Created by | Authoritative? |
|---|---|---|
| `prism-brain.db` | `bridge.bootstrap_workspace()` | Yes (sole truth) |
| `prism/prism-inbox/`, `prism/prism-sources/`, `prism/prism-extensions/` | Same | n/a (containers) |
| `prism-axioms.md` | `regenerate_axioms_projection()` | No — read-only projection |
| `prism-graph-explorer.html` | `update_graph_html()` (after first mutation) | No — projection |
| `prism-graph.json` | `export_graph_json()` (after first mutation) | No — projection |

The bootstrap **does not** copy any starter template — there is no `domains-starter.json` or `AXIOMS-template.md` in the repo any more. Domains and axioms emerge from ingestion + conversation; the projection regenerates after every revision.

### Core MCP tools (manifest.json — 22)

```
prism_core_search        prism_core_ingest        prism_core_inbox         prism_core_ingest_url
prism_core_ingest_text   prism_core_reingest      prism_core_source_delete prism_core_tag
prism_core_graph         prism_core_stats         prism_core_export        prism_core_index
prism_core_domains_get   prism_core_domains_set   prism_core_axioms_get    prism_core_axioms_revise
prism_core_axioms_history
prism_core_extract_concepts prism_core_propose_concept prism_core_review_proposals
prism_core_accept_proposal  prism_core_reject_proposal
```

Extension tools (51Folds: `prism_folds_refine_thesis`, `prism_folds_create`, `prism_folds_status`, `prism_folds_ingest_results`) live in `extensions/51folds/manifest.json` — they are **not** declared in the core manifest.

### Restricted brain API (extensions)

Extensions receive a sanctioned surface, never raw SQL access:

| Method | Purpose |
|---|---|
| `search(query, top_k)` | Hybrid search |
| `addNode`, `addEdge` | Graph mutation |
| `getGraphData` | Read graph |
| `exportGraph` | Trigger projection regeneration |
| `stats` | Database statistics |
| `predictionSave`, `predictionGet`, `predictionUpdate`, `predictionList`, `predictionIngestNarrative` | Prediction CRUD (table in core; 51Folds extension owns orchestration) |
| `ingestText` | Register narrative output as a brain source |

---

## Build History

### Phase 1–7 (original build, v1.0)

The v1.0 build plan called for seven phases (engine refactor, MCP server, templates + first-run, LLM concept extraction, reference brain, extension architecture, polish + package). All seven shipped, but several deviated from the methodology — notably, the `prism-bootstrap` skill shipped as a corrupt zip blob, the docs overclaimed auto-watch / auto-search behaviour the MCP server cannot deliver, the 51Folds extension was a stub, and the onboarding gated value behind articulation. Those issues are documented in the root-level `PLUGIN-CORRECTIONS.md`.

### Remediation phases A–C (v1.0-remediated, current)

| Phase | Item | Result |
|---|---|---|
| **A1** (P0) | Replace corrupt `prism-bootstrap/SKILL.md` blob | UTF-8 markdown skill: session init, retrieval guidance, ingestion dispatch, citation rules. Zero private content. |
| **A2** (P0) | Strip auto-watch / auto-search overclaims | `docs/PROTOCOL.md`, `docs/PLUGIN-HOW-IT-WORKS.md` rewritten honestly: ingestion is user-invoked, search is Claude-invoked. |
| **A3** (P0) | Rewrite README opening | Leads with universal-recall benefit and multi-modal ingestion (files / URLs / pasted text). |
| **A4** (P0) | Rebuild dist with UTF-8 guard | `scripts/package.sh` now fails loud if any staged `SKILL.md` is not UTF-8 — catches the v1 bug class. |
| **B1** (P1) | Port real 51Folds integration | Lean v1: `client.js` (HTTP), `tools.js` (4 tools), `manifest.json`, `.env.example`. Dud detection and monitor view tracked as deferred GitHub issues (#3, #4). |
| **B2** (P1) | Rebuild onboarding | `prism-starter` rewritten: ingest-first; Phase 0 (write your prism) gone; ingestion never blocks for Q&A. |
| **B3** (P1) | Replace 10 hardcoded domains | `_DEFAULT_DOMAINS` emptied; `templates/domains-starter.json` deleted; classifier uses the `domains` table or returns "unclassified". |
| **B4** (P1) | Move domains and axioms into `prism-brain.db` | New tables with revision history; new MCP tools `prism_core_domains_get/set` and `prism_core_axioms_get/revise/history`. `prism-axioms.md` is a read-only projection. |
| **B5** (P1) | Multi-modal ingestion + folder-aware inbox | New tools `prism_core_ingest_url`, `prism_core_ingest_text`. Subfolder names in `prism/prism-inbox/` seed/select domains during classification. |
| **B6** (P1) | Source lifecycle | `prism/prism-sources/` is a read-only archive; new tools `prism_core_reingest`, `prism_core_source_delete`. The DB owns content. |
| **B7** (P1) | Reconcile manifest vs server | `manifest.json` lists all 22 core tools; extension tools live in extension manifests. |
| **B8** (P1) | Predictions location | `predictions` table stays in core; tooling moves into the 51Folds extension via the expanded brain API. |
| **B9** (P1) | Graph Explorer cluster Q&A | Domain detail panel adds "Refine label / keywords" and "Review cluster members" buttons that copy a Cowork prompt to the clipboard for Claude to action via `prism_core_domains_set` / `prism_core_tag`. |
| **B10** (P1) | GitHub issues for deferred work | Issues #3 and #4 filed for dud detection and monitor port. |
| **C1** (P1) | Extend `prism-companion` | Five coaching sections added: citation tests, boundary discipline, confidence review observations, concept extraction QA, retrieval decision tree. |
| **C2** (P1) | Split PROTOCOL.md | Architecture-only (workspace structure, layer summary, settled decisions). Behavioural guidance lives in skills. |

### Deferred (P2 — not blocking)

- Fuzzy node search (`brain_find_node` by semantic match).
- Stats trend surfacing (read meta-table snapshots).
- Programmatic axiom citation parsing (engine-side check at write time exists; deeper analysis of citation graphs does not).
- 51Folds dud detection (issue #3) and monitor view (issue #4).
- Cowork-side honouring of `postMessage` from the Graph Explorer iframe (would replace the clipboard-relay UX with direct Claude routing).

The "long-running Python daemon" item that originally sat here shipped in commit `3566cd8` — the bridge runs in `--daemon` mode by default, with NDJSON request multiplexing and circuit-breaker fallback to spawn-mode after repeated daemon deaths.

---

## Settled Architectural Decisions (load-bearing)

| Decision | Resolution |
|---|---|
| `prism-brain.db` is the single source of truth | All state in SQLite. Inputs flow in, projections regenerate. |
| Domain inference | No hardcoded list. Folder hints in `prism/prism-inbox/` seed domains; prompt context is honoured; classifier scores against the `domains` table; refinement is conversational. |
| Domains and axioms storage | Tables in `prism-brain.db`. `prism-axioms.md` is a regenerated read-only projection. `domains.json` is no longer read at all — a stray file from an older install is ignored; migrate via `prism_core_domains_set` and delete it. |
| Ingestion flow | Three entry points (`prism_core_ingest`, `prism_core_ingest_url`, `prism_core_ingest_text`). Never blocks for Q&A. Clarification is opt-in (Graph Explorer or `prism-companion` conversation). |
| Brain search invocation | Claude judges per prompt. MCP cannot intercept prompts. The `prism-bootstrap` skill carries the decision rules. |
| Concept extraction | Aggressive with **deferred** review. Review surfaces in Graph Explorer or `prism-companion` conversation, not as a blocking post-ingestion list. |
| Predictions location | Table in core, tooling in `extensions/51folds/`. |
| Extension API surface | Restricted methods (search, addNode, addEdge, getGraphData, exportGraph, stats, prediction CRUD, ingestText). Extensions never get raw SQL. |
| Storage engine | SQLite for v1; cloud Postgres migration path exists behind the MCP abstraction. |

---

## For Future Coding Agents

The companion documents:

- **[PLUGIN-HOW-IT-WORKS.md](PLUGIN-HOW-IT-WORKS.md):** Component-by-component architecture reference. Read this to understand *what* exists.
- **[PLUGIN-NEW-USER-EXPERIENCE.md](PLUGIN-NEW-USER-EXPERIENCE.md):** What the user sees and does on first install. Read this to understand *the experience*.
- **[PROTOCOL.md](PROTOCOL.md):** Architecture protocol (v5.0). Workspace layout, layer summary, settled decisions, framework purity. Behavioural guidance lives in the skills.

Behavioural guidance for Claude lives in the three skills:

- `skills/prism-bootstrap/SKILL.md` — session init, retrieval, ingestion dispatch, citation rules.
- `skills/prism-starter/SKILL.md` — new-user onboarding (ingest-first).
- `skills/prism-companion/SKILL.md` — coaching, observation, challenge.

If you find yourself wanting to add behavioural rules to PROTOCOL.md, stop — that work belongs in a skill. PROTOCOL.md describes architecture only.

---

*This plan is versioned alongside the system it describes. Update it when the architecture changes; behavioural changes go in the skills.*
