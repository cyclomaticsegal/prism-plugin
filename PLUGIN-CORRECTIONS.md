# PRISM Plug-in: Audit, Intent, and Corrections Required

**Audience:** Claude Code, working in this repo.

**Purpose:** Reset your understanding of what this plug-in is meant to do, identify where the current build has departed from that intent, and define what to fix. This document is the single authoritative brief. Read it before touching code.

**Author:** Simon Segal, the system's originator and intended primary user. The plug-in is being prepared for distribution to others; the live, working version it was extracted from is in `/Users/simonsegal/lws/frameworks-of-understanding/` ("FoU"). When the plug-in's behaviour diverges from FoU's, FoU is right by default — it's been used and refined for months. The plug-in extraction was supposed to generalise FoU, not reinvent it.

---

## Where to read for ground truth

Two locations matter:

- **`/Users/simonsegal/lws/frameworks-of-understanding/`** — the live system. `brain.py`, `bootstrap.sh`, the populated `prism-brain.db` (925 chunks, 171 nodes, 193 edges, 14 domains, 86 sources, 3 predictions), `51folds-mcp-server/`, `folds_client.py`, `folds_orchestrator.py`. These are working artefacts. They define what the plug-in should be capable of.
- **`/Users/simonsegal/lws/51Folds/prism-plugin/`** — this repo. The extracted, refactored, packaged plug-in. Where the work goes wrong is described below.

Reference both. When uncertain, prefer FoU's behaviour.

---

## What PRISM is meant to be

PRISM is a Cowork plug-in that turns any folder on the user's computer into their **Frameworks of Understanding** — a personal, structured, searchable extension of memory. The name PRISM refers to the methodology's core idea: a prism refracts incoming material through the user's own framework into structured, connected thinking. But the plug-in's value is not gated on the user articulating that framework upfront. The base value is universal: anyone who reads anything seriously stands to gain.

**The killer benefit:** every question the user asks Claude in a Cowork session is now answered against the full depth of what they have read. Not the gist. The actual passages. The actual nuance. Network effects? Claude pulls Chen on atomic networks, Ridley on cumulative recombination, Thiel on monopoly thresholds, plus whatever else the user has ingested on the topic, and answers using all of it. The user cannot remember all that detail in the moment. The plug-in remembers it for them. This is the value. Everything else is enrichment.

**What enrichment looks like:** as the user accumulates ingested material, patterns emerge. The plug-in surfaces these patterns. Through directed conversation — not hand-typing — the plug-in helps the user name their recurring claims (axioms), identify their domains of interest, and notice the cross-domain bridges that are where their original thinking actually lives. The user authors all of this through dialogue with Claude; the plug-in is the scribe. Axioms and domains are *outputs* of the system, not prerequisites to using it.

---

## What's gone off the rails

The current build deviates from this intent in nine specific ways. All are fixable.

### 1. Bootstrap skill is a corrupt zip blob containing private content (P0, ship-blocking)

`skills/prism-bootstrap/SKILL.md` is not a markdown file. It's a ZIP archive (binary). When unpacked, it contains the original FoU `brain-bootstrap.skill` — Simon's private "Great Inversion" master thesis, four hypotheses about machine population, full Simon-specific operating axioms. The manifest declares this skill as generic session initialisation. The actual content is private intellectual property, and worse, it functionally injects Simon's belief system into any other user's Claude sessions on install.

**How it happened:** the FoU file `PLUGIN-SETUP-DIRECTIONS.md` instructed the extraction session to do `cp brain-bootstrap.skill skills/brain-bootstrap/SKILL.md` — a rename without unzipping. The blob was committed in the initial commit (`cb8a0de`) as binary, then renamed (not refactored) in `cf46d96`. The build plan's item A10 ("remove Simon-specific content") was never executed. The same corrupt blob is in `dist/prism-1.0.0.tar.gz` because `scripts/package.sh` copies the skills directory wholesale.

**Fix:** delete the blob. Write a fresh `skills/prism-bootstrap/SKILL.md` in plain markdown that does what the manifest says — session initialisation: restore the brain, verify tools, show status. Pure operational guidance for Claude. Zero axiom content. No Simon-specific material. Then rebuild the dist (`rm -rf dist/ && bash scripts/package.sh`) and verify with `file dist/prism-1.0.0/skills/prism-bootstrap/SKILL.md` that the result is UTF-8 text. Update `docs/PLUGIN-SETUP-DIRECTIONS.md` (still in FoU) so any future re-extraction unzips before copying.

### 2. Documentation overclaims auto-behaviour the build does not implement (P0)

`docs/PROTOCOL.md` and `docs/PLUGIN-HOW-IT-WORKS.md` claim that the MCP server "detects new files in inbox/ and triggers the ingestion pipeline" and "runs a prism_core_search automatically on every user prompt." Neither is wired up. MCP is a tool-calling protocol; the server cannot intercept prompts. The server does not watch the filesystem. Ingestion is user-invoked: the user says "ingest the inbox" (or paste content, or supply URLs — see item 8) and Claude calls the appropriate tool.

**Fix:** strip the auto-watch and auto-prompt-search language from both docs. Replace with the truthful description: ingestion runs when the user requests it through the prompt. Claude calls `prism_core_search` when a question would benefit from the user's knowledge base. Specific lines to correct: `PROTOCOL.md` §"Adding Knowledge" line ~99; `HOW-IT-WORKS` line ~253 and §"The Workflow"; `HOW-IT-WORKS` settled-decision item 2 ("Auto-search on every prompt: yes, every prompt") is wrong as written and must be reworded.

### 3. The 51Folds extension is a stub, not the integration (P0/P1)

This is the single biggest divergence from intent. The 51Folds extension was the canonical case used to design the extension architecture. Its purpose: enable a user, through a Cowork session with the PRISM brain, to instruct Claude to (a) refine a thesis into a 51Folds-ready hypothesis using the brain as context, (b) generate a fit-for-purpose ~300-word grounding paragraph that gives the 51Folds API enough context to build a useful probabilistic model, (c) submit to the 51Folds API to create the model, (d) poll for completion, (e) ingest the resulting model narrative back into the brain as a new source, and (f) wire the prediction node to the concepts in the brain that informed it.

**What's actually in `extensions/51folds/tools.js`:** ~80 lines that create a local node marked "prediction" and wire it to nearby concepts via search. **No HTTP calls. No 51Folds API. No thesis refinement. No 300-word context generation. No model submission. No polling. No result ingestion.** It is a placeholder demonstrating that the extension architecture works — drop a folder, get a tool, the tool can read and write the graph through the restricted API. As proof-of-architecture, fine. As the canonical 51Folds integration, hollow.

**The real implementation already exists in FoU:**
- `frameworks-of-understanding/51folds-mcp-server/server.js` (337 lines) — calls `api.51folds.ai` and `app.51folds.ai`, handles bearer-token auth, full CRUD, status polling, results fetching
- `frameworks-of-understanding/folds_client.py` (494 lines) — Python API client
- `frameworks-of-understanding/folds_orchestrator.py` (975 lines) — end-to-end pipeline: thesis parsing, dud detection, narrative generation, graph integration, monitor update

**Fix:** port the FoU 51Folds integration into `extensions/51folds/` properly. The extension needs (at least) two tools: one that runs the directed Q&A with the user to refine a thesis and draft the grounding paragraph using the brain as context, and one that submits to the 51Folds API and registers the model in the brain. A status/poll path picks up results when ready and ingests the narrative as a new source, wiring edges to the relevant concepts. Bearer token via `.env` in the extension folder, mirroring the FoU pattern. None of this needs to be written from scratch — it exists. The work is translation into the extension format, not invention.

### 4. The axioms-by-typing onboarding is backwards (P1)

The current `prism-starter` skill walks the user through Phase 0 (write your prism), Phase 1 (define your domains), Phase 2 (drop files), Phase 3 (ingest), Phase 4 (write your axioms). It demands articulation before ingestion. This is the wrong order. Almost no user can articulate their prism on day one — they haven't seen the shape of their reading yet. The ones who can don't need the tool. Asking them is a friction gate that filters out the actual target user.

**The intent:** axioms emerge from a conversation between the reading and the reader, facilitated by Claude. The plug-in is the scribe. The user does not type `prism-axioms.md` directly. After ingestion of an initial batch, the plug-in runs a directed Q&A: "these six ideas show up across four of your domains — there's a claim connecting them, want to name it?" The user reacts, sharpens, redirects, rejects. Claude drafts to `prism-axioms.md` from the conversation, with a revision log entry. The user reviews what Claude wrote and corrects. Authorship is preserved through edits and rejections, not through typing.

**Fix:** rebuild the onboarding so the first thing a user does is ingest, not articulate. The new flow is: drop files → ingest → Claude clusters the ingested material and proposes a starting domain list (user nods or tweaks) → Claude proposes candidate prisms / frameworks / bridges drawn from the actual reading, in a directed Q&A → Claude writes `prism-axioms.md` from the conversation. Phase 0 (define your prism) disappears. Phase 1 (define your domains) becomes inferred by Claude with user confirmation. The companion skill's role shifts from "challenge what you wrote" to "notice when new reading should update what Claude wrote for you" and to surface contradictions when newly ingested material undermines an existing axiom.

### 5. The "10 generic domains" framing is wrong (P1)

`templates/domains-starter.json` ships with 10 fixed, hardcoded "generic" domains (Economics, Technology, Political Economy, etc.). This contradicts the methodology. There is no fixed number. There are *N* domains, where N is whatever the user's reading reveals. They are not "generic" — they are domains of interest, frameworks of understanding. That's the whole reason the product is called PRISM.

**Fix:** replace the `domains-starter.json` model with inferred domains. After the first batch is ingested, Claude clusters the sources, proposes a starting set (could be 6, could be 14, depends on the reading), and runs a brief Q&A to confirm or adjust labels and descriptions. The number of domains is whatever the material supports. The labels are the user's, not a default list. See also item 6 (whether `domains.json` should be a file at all).

### 6. Files used where the database should be authoritative (P1)

The protocol explicitly states `prism-brain.db` is the single source of truth. The current build then carves out two exceptions: `domains.json` (configuration) and `prism-axioms.md` (user content). Both contradict the principle. Configuration should live in the database's `meta` table or a dedicated `domains` table. Axioms should live in a dedicated `axioms` table with revision history, since the methodology specifically calls for old axioms to be marked superseded with a dated rationale.

**Why the build chose files:** `prism-axioms.md` was meant to be hand-edited in a text editor; `domains.json` was meant to be human-readable. Both reasons go away if the user authors through conversation with Claude (item 4) and the plug-in is the scribe.

**Fix:** move domains and axioms into `prism-brain.db`. Expose them through MCP tools — `prism_core_axioms_get`, `prism_core_axioms_revise`, `prism_core_domains_get`, `prism_core_domains_set`. Eliminate `domains.json` and `prism-axioms.md` as workspace files. The "what if something goes wrong" repair scenario in the README (which currently talks about preserving these files) collapses cleanly: there is one database, it's either present or absent, repair is recreate-if-missing. No mixed state. No file-vs-database divergence.

### 7. Ingestion is narrowly defined (P1)

The current build treats ingestion as inbox-only: drop files into `prism/prism-inbox/`, run `prism_core_ingest`, files move to `prism/prism-sources/`. The intent is broader. The Cowork prompt is the universal entry point. Three valid ways to feed the brain:

- Drop files into `prism/prism-inbox/` (current behaviour, fine)
- Paste URLs into the Cowork prompt: "ingest these pages [list of URLs]" — Claude fetches, summarises and synthesises across them, ingests
- Paste raw text into the Cowork prompt: "ingest this transcript [...content...]" — Claude treats the pasted content as a source, ingests

In all three cases, Claude is the dispatcher that recognises the user's instruction and calls the appropriate ingestion tool. The unifying mental model: the prompt is the universal ingestion entry point. The inbox is one of three valid forms of input.

**Folder structure inside the inbox should also be meaningful.** If files are grouped under sub-folders, the folder names are instructive about the nature of the material — they should influence domain inference. A folder called `network-effects/` with 12 PDFs in it tells the classifier something. The current ingestion code reads the inbox flat and ignores structure.

**Fix:** add `prism_core_ingest_url` and `prism_core_ingest_text` tools alongside the existing `prism_core_ingest` (which still reads `prism/prism-inbox/`). Update the inbox processor to read sub-folders and use folder names as classification hints. Update `prism-bootstrap/SKILL.md` (item 1) to teach Claude when to call which ingestion tool based on what the user pastes or asks.

### 8. Post-ingestion file lifecycle is undefined (P1)

After files in `prism/prism-inbox/` are processed, they move to `prism/prism-sources/`. The protocol says this. But the README's "what if something goes wrong" repair scenario says PRISM preserves files the user has edited — without specifying whether `prism/prism-sources/` files are part of that protected set. What if a user re-edits a source file after it's been ingested? Does the engine re-ingest? Detect the change? Ignore it because the chunks are already in the database? What if they delete a file from `prism/prism-sources/`? Does the corresponding node disappear from the graph? Can the user re-ingest a single source without re-running the whole inbox? The current build has no clear answer.

**Fix:** define and document the lifecycle. My recommendation: once ingested, files in `prism/prism-sources/` are read-only archived copies. The database owns the content. Editing or deleting a source file does not change the brain. To re-ingest, the user explicitly invokes a tool (`prism_core_reingest <source_id>`) that re-reads the source file, regenerates chunks/embeddings, and updates the graph. Deleting a source from the brain is a database operation, not a file operation. This makes the file/database boundary unambiguous.

### 9. The README undersells the product (P0/P1)

The README opens with: "PRISM is a personal knowledge base that turns your reading into connected, searchable thinking." This is correct but narrow. PRISM is your *frameworks of understanding* — a personal, structured, searchable extension of memory that gets richer with every session and that makes every AI conversation deeper because it carries the full depth of your reading. The README should lead with that benefit, not the file-cabinet framing. Keep the simple "drop files, search them" capability description, but as the floor of what PRISM does, not the ceiling.

**Fix:** rewrite the README opening to lead with the universal-recall benefit and the frameworks-of-understanding methodology. Be explicit that ingestion is multi-modal (files / URLs / pasted text via the Cowork prompt). Replace the "first launch creates these files" list with the corrected post-fix-6 architecture: first launch creates the database, the user starts ingesting, axioms and domains emerge through conversation. Strip the misleading auto-search and auto-ingest claims (item 2).

---

## Priority order for fixes

**P0 — fix before any further distribution or testing:**

1. Replace the corrupt `prism-bootstrap/SKILL.md` blob with a clean operational skill (item 1)
2. Rebuild `dist/` and verify the package no longer carries Simon's private content (item 1)
3. Strip the auto-search and auto-ingest overclaims from `PROTOCOL.md` and `PLUGIN-HOW-IT-WORKS.md` (item 2)
4. Rewrite the README opening to reflect actual intent (item 9)

**P1 — fix before any external user installs:**

5. Port the real 51Folds integration from FoU into `extensions/51folds/` (item 3) — the single largest task; the code mostly exists in FoU and needs translation into the extension format
6. Rebuild onboarding: ingest first, articulate later, axioms drafted by Claude through Q&A (item 4)
7. Replace "10 generic domains" with N inferred domains (item 5)
8. Move domains and axioms from files into `prism-brain.db`, expose via MCP tools (item 6)
9. Add URL ingestion and pasted-text ingestion tools; teach the bootstrap skill when to use each (item 7)
10. Make the inbox processor folder-aware so folder names inform domain classification (item 7)
11. Define and document the post-ingestion file lifecycle (item 8)
12. Reconcile `manifest.json` (lists 8 tools) with the server (registers 14)
13. Decide: predictions in the core, or in the extension. Right now they're in both. Pick one. The cleaner choice is to move all prediction-specific code out of `brain.py` into the 51Folds extension, since prediction is conceptually an extension concern.

**P2 — investments if continuing development:**

14. Replace subprocess-per-call bridge with a long-running Python daemon for latency
15. Add fuzzy node search (`brain_find_node` by semantic match on labels)
16. Surface stats trends (you already write snapshots to the meta table after every ingestion; nothing reads them)
17. Parse axiom citations programmatically so the companion skill can verify them as code rather than reading prose

---

## Philosophy to preserve through the fixes

A few load-bearing principles. When in doubt, lean on these:

- **The database is the truth. Files are inputs and outputs only when there's no alternative.** Files in `prism/prism-inbox/` are inputs. The graph explorer HTML is an output. Domains, axioms, predictions, source registry: database. No exceptions for "user-editable" files — the user edits through Claude.

- **The Cowork prompt is the universal ingestion entry point.** Files, URLs, and pasted content all flow in through Claude recognising the user's instruction and calling the right tool. The inbox is one of three valid forms of input, not the only one.

- **Axioms emerge from conversation, not authorship.** The user does not type `prism-axioms.md`. The user argues with Claude until they land somewhere both agree captures the user's actual position. Claude writes the file. Authorship is preserved through edits and rejections.

- **Domains are *N*, not 10.** Whatever the user's reading reveals. Inferred from clustering, named through Q&A, never hardcoded.

- **The killer benefit lands at first ingestion, not at first axiom.** The user gets value the moment Claude starts answering their questions against their reading. Axioms and frameworks are enrichment. Don't gate the value on enrichment.

- **Extensions specialise, the core stays small.** The 51Folds case is canonical: real API integration, real model creation, real result ingestion, but only through the restricted brain API (`search`, `addNode`, `addEdge`, `getGraphData`, `exportGraph`, `stats`). The core never depends on any extension. Extensions add capability without coupling.

- **When the build deviates from FoU's working behaviour, FoU is right by default.** The plug-in is a generalisation of a working system, not a reinvention. If a feature works in FoU and is broken or absent in the plug-in, the plug-in is wrong.

---

*End of brief. Read `frameworks-of-understanding/` before writing code. Begin with P0.*
