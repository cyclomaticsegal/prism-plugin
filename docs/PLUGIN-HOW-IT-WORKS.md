# How PRISM Works

**Purpose:** Explain every component of the PRISM system in plain language, with enough technical detail to make architecture decisions. If you understand this document, you can brief a developer.

**Note on naming:** PRISM was developed under the working title "Frameworks of Understanding." References to "the brain" throughout refer to the user's PRISM instance — their personal knowledge base.

**Companion document:** [PLUGIN-NEW-USER-EXPERIENCE.md](PLUGIN-NEW-USER-EXPERIENCE.md) covers what a first-time user sees and does.

---

## The System in One Sentence

A personal knowledge base that stores everything you read and think, connects ideas across domains, lets you search your own thinking, and shows you the shape of your intellectual world as an interactive map.

---

## The Components

### 1. The Database (prism-brain.db)

The single source of truth. All state lives here. The workspace's `prism-axioms.md` is a regenerated read-only projection of the `axioms` table — not an authoritative file the user hand-edits.

**What it stores:**

Nine tables. Chunks (source material broken into searchable pieces of ~2,000 characters each). Nodes (domains, concepts, sources, predictions). Edges (typed connections — `contains`, `relates_to`, `sourced_from`, `exemplifies`, `enables`, `causes`, `quantifies`, etc.). Embeddings (one numerical fingerprint per chunk). Domains (label, short_label, color, keyword description for the classifier). Axioms (with revision history via `superseded_by` / `superseded_at`). Predictions (registry for the 51Folds extension's models). Meta (key-value config and stats snapshots). And `chunks_fts`, an FTS5 virtual table for BM25 keyword search.

**How concepts get discovered and connected:**

Source and domain nodes are created by the engine during ingestion. **Concept nodes are created by Claude**, immediately after ingestion completes, driven by the `prism-bootstrap` skill: for each new source, Claude calls `prism_core_extract_concepts` to read the chunks and current graph structure, then `prism_core_propose_concept` per identified concept (with typed edges to existing nodes). The user reviews via `prism_core_review_proposals` and accepts/rejects. This is a non-deterministic step — language judgment lives in the skill, not in `brain.py` — but it is wired into the ingestion flow and runs every time, not occasionally or on request.

**How search works:**

Two parallel searches, blended.

Keyword search uses BM25 via SQLite's FTS5 (full-text search) engine. This matches exact words, weighted by how distinctive each word is. Common words count less. Rare words count more.

Semantic search uses cosine similarity between the embedding of your question and the embedding of every chunk. The primary embedding model is sentence-transformers (all-MiniLM-L6-v2, 384-dimensional vectors). When that model is not available (proxy issues in the Cowork VM), it falls back to TF-IDF (term frequency, inverse document frequency) using scikit-learn. TF-IDF is not true semantic search but it captures term importance and distributional similarity well enough.

The two result sets are blended using Reciprocal Rank Fusion (RRF). Each result gets a score based on its rank in both lists: `score = 1/(k + rank)`, where k=60 is a smoothing constant. Results that rank well in both searches rise to the top. This catches things that either search alone would miss.

No threshold for inclusion. The engine returns the top-k results (currently 10, plugin default will be 3 for context budget discipline). Every result includes the source title, domain, relevance score, and a text excerpt.

**SQLite: is it sufficient?**

For a personal knowledge base, yes. SQLite handles databases up to 281 terabytes in theory. In practice, performance is smooth up to around 1-2 GB, which represents thousands of sources and hundreds of thousands of chunks. Simon's brain (45+ sources, 600+ chunks, 170+ nodes) is 2 MB. A brain ten times that size would be 20 MB. A brain a hundred times that size would be 200 MB. SQLite handles all of these without breaking a sweat.

Postgres would be the right move if: the brain needed concurrent multi-user access (it does not, it is personal), the database exceeded 1-2 GB (unlikely for personal use), or the user wanted server-based remote access. For the plugin's target use case (one person, one brain, local-first), SQLite is the correct choice. It is a single file, zero configuration, no server process, and it ships with Python.

**Future scaling path:**

If a user's brain outgrows SQLite (which would require an extraordinary volume of ingested material), the upgrade path is migration to a cloud-hosted Postgres instance (e.g., Supabase, Neon, AWS RDS, or similar managed service). Not local Postgres. If you have outgrown a local file, you have outgrown local infrastructure. The upgrade goes from "single file on your machine" to "managed database in the cloud." No intermediate step of running a Postgres server on your laptop.

The MCP server abstracts the storage layer. Swapping the backend from SQLite to cloud Postgres behind the MCP interface would not change anything the user sees. Same tools, same search, same graph, same explorer. The database just lives somewhere else and handles more volume. This also opens the door to cross-device access (your brain available from any machine with Cowork installed) and backup redundancy (cloud provider handles durability).

This is a future concern, not a plugin v1 concern. But the MCP abstraction layer makes the migration straightforward when the time comes.

---

### 2. The Engine (brain.py)

The engine is the only thing that talks to the database. Currently 2,000 lines of Python. It handles six operations.

**Ingest**

You drop files into the inbox folder. The engine processes them. Here is exactly what happens:

1. The engine scans prism-inbox/ for supported file types (.md, .txt, .pdf). Other file types are ignored. Encrypted files are skipped.
2. Each file is read and broken into chunks of approximately 2,000 characters (~500 tokens), with 256 characters of overlap between consecutive chunks. The overlap ensures that ideas split across chunk boundaries are not lost. Both chunk size and overlap are configurable constants in the engine.
3. Each chunk gets a content hash (first 500 characters, SHA-256). If a chunk with the same hash already exists in the database, it is skipped. This prevents duplicate ingestion if the same file is dropped into the inbox twice.
4. Each chunk gets an embedding (numerical fingerprint). The embedding model is configurable: sentence-transformers for true semantic vectors, TF-IDF as fallback. Embeddings are stored in the database and persist across sessions. They are not regenerated unless the source is re-ingested.
5. The classifier scores the source against domain keyword descriptions using TF-IDF cosine similarity and assigns the top two domains that exceed a confidence threshold of 0.05. Low-confidence classifications are flagged.
6. The engine creates a source node in the graph and wires it to the assigned domain nodes.
7. All outputs (graph explorer, graph data) are regenerated from the database.
8. The processed files are moved out of prism-inbox/ into prism-sources/.

**What runs after the engine returns:** the `prism-bootstrap` skill drives Claude through the post-ingestion concept-extraction loop (see Database section above) — `prism_core_extract_concepts` per new source, then `prism_core_propose_concept` per identified idea, then a brief offer of `prism_core_review_proposals` to the user. Token-limit management for very long sources is the skill's responsibility (chunk the extraction prompt across batches if needed).

**What the engine handles itself:** transactional ingestion with per-source rollback (a chunk-or-embed failure rolls back that source cleanly, never leaving a half-ingested record). Large PDFs are batched page-by-page once they exceed the configured threshold. Failures are surfaced to Claude as bridge errors so the skill can decide whether to continue or stop.

**Search**

You type a question in the Cowork prompt box. The MCP server intercepts it. Here is what happens:

1. The engine runs a BM25 keyword search against the FTS5 index.
2. Simultaneously, it generates an embedding for your question and runs cosine similarity against every stored embedding.
3. The two result sets are blended via RRF.
4. The top results (default 3 for plugin, configurable) are returned as structured data: chunk ID, source title, domain, relevance score, short excerpt.

**How search integrates with Claude:**

This is the critical design question. The brain's search results become context that informs Claude's response. The MCP server returns them as tool results. Claude sees them alongside whatever else it knows from its training data and from any web search it performs.

The blending works naturally through Claude's own reasoning. When the user asks a question, the brain provides domain-specific context from the user's own knowledge base. Claude's training data provides general knowledge. If the brain has strong results (high relevance scores, multiple matching sources), those dominate the response. If the brain has weak or no results, Claude falls back on its training data and can search the web.

The plugin should not try to build a complex arbitration system for when to use brain results vs training data vs web search. Claude already does this. The MCP server's job is to surface the brain's results reliably. Claude's job is to synthesise them with everything else it knows. Trust the model.

A common misconception: that the MCP server intercepts every prompt and silently runs a brain search. It does not. MCP is a tool-calling protocol — Claude calls `prism_core_search` when it judges a question benefits from the user's reading. The decision lives in the `prism-bootstrap` skill (retrieval guidance). The user does not need to type a special command, but they should not assume the brain is consulted on every reply either; if Claude doesn't search, the response is general training-knowledge.

**Tag**

Manual correction of domain classification. Currently requires the user to know the source ID (like "S41") and the domain numbers (like "10,12"). The plugin replaces this with two opt-in surfaces — never a blocking flow at the moment of ingestion:

1. **Graph Explorer cluster labelling.** Low-confidence classifications and unlabelled clusters are visually flagged in the explorer. Click a cluster → a side panel surfaces a summary and a "name this cluster" / "reassign this source" exchange. The change writes back to `prism-brain.db` and reclassifies.
2. **Companion-skill observations.** When the user is in conversation with Claude, the `prism-companion` skill surfaces low-confidence cases opportunistically ("Source S07 was classified to D03 with low confidence — does that fit, or should the domain description be tightened?"). The user accepts or revises; the engine writes through.

No raw IDs. No typing domain numbers. No prompt that interrupts ingestion to demand corrections.

**Graph**

Shows what a specific idea is connected to. Start at any node, see everything within one hop, two hops, or more.

This is not Neo4j. The graph is stored as two tables in SQLite (nodes and edges). The engine traverses the graph by running SQL joins. For the scale of a personal knowledge base (hundreds of nodes, hundreds of edges), this is fast. Graph query performance degrades at tens of thousands of nodes, which is well beyond personal use.

The graph is queryable through the engine's graph command. Fuzzy search on the graph (finding nodes by approximate name match) is not currently supported but would be a useful addition. The engine could match node labels against the query using string similarity or, better, embed node labels and search semantically.

The graph is persisted in the database. prism-graph.json is a projection (a snapshot exported for the visualisation layer). The graph explorer HTML is another projection. Both are regenerated from the database after every mutation. They do not need to be maintained separately.

**Stats**

Current metrics: source count, chunk count, node count, edge count, domain distribution, embedding count.

**Currently included** in `prism_core_stats`: source/concept/node/edge/chunk/embedding counts, domain count, cross-domain edge count, last ingestion timestamp, breakdown by node type and edge type, db size. Stats snapshots are written to the `meta` table after every ingestion (the time series exists; nothing currently reads it).

**Useful additions still on the list:** ingestion history surfaced as a chart; domain density comparison (which domains are rich, which sparse); stale source detection (sources never surfacing in search results); axiom coverage (which axioms cite which concepts, and which concepts are uncited). These would mostly be served by reading the existing meta-table snapshots — a P2 enrichment, not new engine work.

**Export**

Regenerates all output files from the database. In the plugin, this runs automatically after every database mutation. The user never triggers it manually.

The outputs are: prism-graph-explorer.html (the interactive map), and prism-graph.json (the graph data that the explorer consumes). The index is no longer a file. It is a database query.

---

### 3. The Knowledge Graph

The structure that makes this more than a filing cabinet.

**Schema:**

Four node types: domain, concept, source, prediction. Four edge types: "contains," "relates_to," "sourced_from," "exemplifies." The schema allows additional edge types. Adding a new type requires no schema change. Edges have a type field (text) and a label field (text). You can add "contradicts," "extends," "causes," "depends_on," or any other relationship by simply using it in a new edge.

New edge types do not need to be "trained." The graph does not learn edge types. It stores them as labels. The intelligence about what relationships mean lives in Claude (which interprets them during search and synthesis) and in the user (who creates or approves them).

**How concepts get connected:**

The engine creates source and domain nodes during ingestion. The `prism-bootstrap` skill drives Claude through the post-ingestion concept-extraction loop: for each new source, `prism_core_extract_concepts` returns the chunks plus existing graph context, Claude identifies discrete claims/frameworks, and `prism_core_propose_concept` writes them with typed edges. The user reviews via `prism_core_review_proposals`. Over time, the graph self-organises into the user's intellectual structure.

**How domains work:**

Domains are clusters. Each domain is a node with a group_id. Every concept and source node also has a group_id linking it to a domain. The graph explorer colours nodes by group_id. The legend filters by group_id. You define eight to fourteen domains. The classifier assigns new sources to domains automatically.

**Cross-domain edges:**

An edge between two nodes in different domains is a cross-domain bridge. These are where the real value lives. The structural heuristics feature should count these and surface the absence: "You have 40 nodes across 10 domains but only 2 cross-domain edges. Your domains are islands."

**How syntheses are surfaced:**

The engine doesn't surface them on its own — the graph explorer makes them visible if you look, and the `prism-companion` skill flags them conversationally ("C22 connects to three domains — what's the bridge?"). This is a judgment task, not an engine task: the engine exposes the graph data; Claude interprets it through the companion skill.

---

### 4. The Graph Explorer

An interactive map of the knowledge graph. Opens in a browser or in the Cowork canvas when selected from the file sidebar.

Built with D3.js (a JavaScript visualisation library). Force-directed layout (nodes repel each other, edges pull connected nodes together, the result is an organic map where clusters form naturally). Every node is a dot. Every edge is a line. Domains have colours. The legend at the bottom lets you click domains on and off.

**Clicking behaviour:** Clicking a node shows its metadata in a detail panel: label, type, domain, connected nodes.

For concept and source nodes the panel offers two action buttons — "Summarise" and "Ask a question" — which `postMessage` the node's context up to Cowork (when loaded as an iframe) and fall back to clipboard-copy + alert (when opened in a normal browser).

For domain nodes the buttons are "Refine label / keywords" and "Review cluster members" — both copy a Cowork-ready prompt to the clipboard so the user can paste it back into Cowork and have Claude apply the change via `prism_core_domains_set` or `prism_core_tag`. The clipboard relay is what static `file://` HTML can deliver; a tighter UX (clicking a node and getting an immediate Claude response) needs Cowork to honour `postMessage` from the iframe directly. That's a Cowork-side feature, not a plug-in change.

**Persistence:** The explorer is a projection. It is regenerated entirely from the database after every mutation. prism-graph.json (the data file the explorer reads) is also a projection. Neither is a source of truth. Both could be eliminated if the explorer queried the database directly, but for the plugin the projection pattern is simpler (the HTML file is self-contained, works offline, and opens in any browser).

**Performance:** The D3.js force simulation handles hundreds of nodes smoothly. Performance degrades above roughly 2,000-3,000 nodes. For a personal knowledge base, this is not a concern. If it ever becomes one, the explorer can switch to a canvas-based renderer (like sigma.js) instead of SVG.

---

### 5. The Domain Classifier

Assigns new sources to domains automatically during ingestion.

**How it works today:**

Each domain has a keyword description (a paragraph of 20-50 words listing the key terms for that domain). The classifier builds a TF-IDF matrix from all domain descriptions plus the new source's text. It computes cosine similarity between the source vector and each domain vector. The top two domains above a threshold of 0.05 are assigned.

This is not using an LLM. It is statistical text matching. It works well when the keyword descriptions are well-written and the source material uses similar vocabulary. It struggles when the language is different (a source about "fiscal policy" might not match a domain described with "government spending" unless both terms appear in the description).

**What the plugin changes:**

Domain descriptions move from hardcoded constants into the `domains` table inside `prism-brain.db`, addressed through MCP tools (`prism_core_domains_get`, `prism_core_domains_set`). The classifier, the graph, and the explorer all read from the database. There is no separate configuration file to keep in sync.

**How new users get to a working set of domains:**

There is no fixed domain count. The number is whatever the user's reading reveals.

Inference order (no blocking Q&A during ingestion):

1. **User-supplied specification wins.** Subfolder names within `prism/prism-inbox/` become domain hints — and seed new domains in the `domains` table if they do not yet exist. Direction inside the user's prompt ("ingest these as part of my political-economy reading") is honoured as explicit.
2. **Otherwise the classifier runs against the existing `domains` table.** Low-confidence results land as-is. Ingestion does not stop to ask.
3. **Post-hoc clarification belongs in the Graph Explorer.** Unlabelled clusters and low-confidence sources are visually flagged. The user clicks a cluster, sees the summary, and names or refines the domain. The change writes back to the table and the cluster reclassifies. The `prism-companion` skill also surfaces low-confidence cases conversationally when the user is engaged with Claude.

Over time the descriptions tune themselves: each cluster-labelling exchange and each conversational correction updates the relevant `domains` row.

**Confidence scores:**

The classifier produces cosine similarity scores for every domain. These should be exposed to the user as confidence percentages. A source classified into "AI" with 0.45 similarity and "Energy" with 0.08 similarity is a confident classification. A source classified into "AI" with 0.12 and "Innovation" with 0.11 is uncertain. The user sees the difference and knows which ones to check.

---

### 6. The Inbox Pipeline

Drop files into the inbox folder. The engine does everything else.

**Chunk sizes:** ~2,000 characters per chunk (~500 tokens), with 256 characters of overlap. These are configurable. Smaller chunks produce more granular search results but lose context. Larger chunks preserve context but dilute search precision. 500 tokens is a well-tested sweet spot for RAG systems.

**Supported formats:** Markdown (.md), plain text (.txt), PDF (.pdf). Encrypted files and unsupported formats are skipped with a warning.

**Large files:** PDFs are read page by page. Very large PDFs (100+ pages) should be processed in batches to manage memory. This is not currently implemented and should be added for the plugin.

**Duplicate detection:** Content hash on the first 500 characters of each chunk. If a match exists, the chunk is skipped. This catches the same file dropped in twice under different names.

**Confidence scores:** Stored on every classification. Surfaced opt-in — visually in the Graph Explorer (low-confidence items flagged for cluster-level Q&A) and conversationally through the `prism-companion` skill. Never as a blocking review list at ingestion time.

**The index is not a file.** In the current system, INDEX.md is a markdown file generated alongside the database. This is redundant. If the database is the single source of truth, the index is a database query. The plugin replaces INDEX.md with a `prism_core_index` MCP tool that returns the source registry on demand. No loose files.

**Error handling:** Each source's ingestion is wrapped in a SQLite SAVEPOINT (`_ingest_single_source` in `brain.py`). If chunking, embedding, or classification fails for a source, the transaction rolls back cleanly — no orphaned chunks, no half-ingested record. The failure is reported back through the bridge so the calling skill can decide whether to continue with the next source.

---

### 7. The Bootstrap

A technical necessity, invisible when it works.

SQLite requires a real filesystem for its write-ahead logging (WAL) mode. Cowork mounts the user's folder via FUSE (a filesystem bridge), which does not support WAL. So the engine copies prism-brain.db from the mounted folder into the session VM at startup, operates on the local copy, and copies back after every write operation.

This is automatic. The user never sees it. It is the reason SQLite remains the right choice over Postgres for the plugin. Postgres would require a running server process. SQLite just needs a file copy.

---

## The Workflow

Here is exactly what happens when you use the brain, step by step.

### Adding Knowledge

1. You add material in any of three ways: drop files into the inbox folder (accessible from Cowork's file sidebar), paste URLs into the prompt, or paste raw text into the prompt.
2. You request ingestion ("ingest the inbox", "ingest these URLs: ...", "ingest this passage: ..."). Claude recognises the intent and calls the matching tool — `prism_core_ingest`, `prism_core_ingest_url`, or `prism_core_ingest_text`. The MCP server does not watch the filesystem; nothing fires until you ask.
3. The engine reads each input, breaks it into chunks (2,000 characters, 256-character overlap).
4. Each chunk gets an embedding (numerical fingerprint for semantic search).
5. The classifier scores each source against your domain descriptions and assigns the top matches. Subfolder names within `prism/prism-inbox/` and any prompt context you supplied are honoured as classification hints.
6. Claude reads the chunks and may extract key concepts, creating concept nodes and proposing edges to existing nodes.
7. The engine writes everything to the database.
8. The graph explorer and graph data file are regenerated from the database (this part is automatic — projections always rebuild after a mutation).
9. Low-confidence classifications are recorded but do not block. You review and refine later — in the Graph Explorer (cluster labelling) or in conversation with Claude (companion-skill observations). Ingestion itself is never a Q&A flow.

### Searching

1. You type a question in the Cowork prompt box. Any question. No special syntax.
2. Claude evaluates whether the question depends on your specific reading. The decision rules live in the `prism-bootstrap` skill (working memory first; brain search for depth questions; direct file read for structural questions; training knowledge for general background).
3. If a brain search is warranted, Claude calls `prism_core_search`. The engine runs keyword search (BM25) and semantic search (cosine similarity) in parallel and blends them via RRF.
4. The top results return as structured tool data: source title, domain, relevance score, excerpt.
5. Claude synthesises the results into the response, citing source IDs. If no relevant chunks come back for a question that should hit the brain, Claude says so explicitly.
6. If the question doesn't depend on your reading, Claude answers from training knowledge without invoking the brain — and says so plainly.

If you want raw search results, ask: "What does my brain say about X?" Claude will call the tool and surface the chunks directly.

### Correcting

No raw source IDs. No domain numbers. Correction is opt-in and lives in two surfaces — never a blocking review at ingestion time.

**Graph Explorer cluster labelling.** Low-confidence sources and unlabelled clusters are visually flagged in the explorer. Click → a side panel surfaces a summary (top concepts, source titles, sample chunks) and a name/refine input. The user names the cluster or reassigns the source, the change writes through to `prism-brain.db`, the graph rewires.

**Companion-skill observations.** When the user is in conversation with Claude, the `prism-companion` skill surfaces low-confidence cases opportunistically: "This source about semiconductor supply chains was classified under AI with low confidence. It looks more like Energy & Infrastructure. Move it, or split between both?"

Both paths share three options at the point of correction: accept, reassign (one domain), or split (multiple). The mechanism differs (UI click vs conversation), but neither stops the user mid-ingestion to demand judgment.

### Exploring

1. You open the graph explorer. It is available in two places: as an HTML file that opens in a browser, and as a canvas view inside Cowork when you select it from the file sidebar.
2. You see your knowledge graph as an interactive map. Nodes are dots, edges are lines, domains have colours.
3. You click domains in the legend to show or hide them.
4. You click a node. The detail panel shows its connections and metadata.
5. (Plugin addition) Below the detail panel, two buttons: "Summarise" and "Ask a question." Summarise sends the node's context to Claude and returns a synthesis of that concept and its connections. Ask a question opens a prompt field pre-loaded with the node's context, letting you interrogate a specific part of your knowledge graph conversationally.
6. The explorer is always current. Every database change regenerates it automatically.

---

## What Changes for the Plugin

The brain currently runs as a Python script (brain.py) that you interact with by typing commands in a terminal. The plugin wraps this so that Cowork can use it directly, and so that other people can install and use their own version. Here is what each change means in plain language.

### Change 1: The MCP Server

MCP stands for Model Context Protocol. It is the standard way that Cowork plugins communicate with Claude. Think of it as a translator.

Today, to search your brain, Claude runs a bash command: `python3 brain.py search "your question"`. It reads the text output. This is fragile. String parsing, error handling, no structure.

The MCP server replaces this. It is a small program that runs in the background during your Cowork session. Cowork sends it structured requests ("search for X with these parameters"). The server calls brain.py's functions directly (no command line) and returns structured data (JSON objects with typed fields). Claude receives clean, reliable data instead of parsing terminal output.

The server is thin. All the intelligence stays in brain.py. The server handles: translating Cowork's requests into brain.py function calls, returning structured results, triggering automatic actions (like regenerating the explorer after a database change), and discovering any installed extensions.

The tools the server exposes: prism_core_search, prism_core_ingest, prism_core_inbox, prism_core_tag, prism_core_graph, prism_core_stats, prism_core_export.

### Change 2: Database as Single Source of Truth

Today, the brain has a mix of files that sometimes contain the authoritative version of something and sometimes are just copies. INDEX.md is generated from the database but also sits as a file. prism-graph.json is exported from the database but also sits as a file. This creates confusion about which version matters.

The plugin makes it simple. The database is the truth. Everything else is either an input (files you drop into the inbox) or an output (the graph explorer, the graph data file, any reports). Inputs flow into the database. Outputs are generated from the database. One direction. No ambiguity.

INDEX.md is eliminated as a file. The index becomes a database query, available through the prism_core_index MCP tool.

prism-axioms.md is the one exception. It is human-authored, not generated. The user writes it. The database reads it (for the Socratic companion to analyse). It lives in the workspace as a markdown file because it is a document you edit in a text editor, not a database record.

### Change 3: Automatic Explorer Updates

Today, you have to run `brain.py export-graph` to update the graph explorer after changes. The plugin removes this step. Every operation that changes the database (ingest, tag, add node, add edge, delete) triggers automatic regeneration of the graph explorer and graph data. The explorer is always current.

### Change 4: Drop-In Extensions

The 51Folds prediction system, and anything like it, should not be part of the core engine. It is specific to one user. The plugin uses an extension architecture.

An extension is a folder containing: a manifest file (declaring what the extension provides and what it needs from the engine), its code, and optionally its own skill file. The MCP server discovers extensions at startup by scanning the extensions/ folder. It registers each extension's tools alongside the core tools.

Installing an extension: drop the folder into extensions/. Removing: delete the folder. Updating: replace the folder with a newer version. The core engine never depends on extensions being present. If an extension is missing, the core works exactly as before.

### Change 5: Domains as a database table

Domains live in the `domains` table inside `prism-brain.db`. There is no hardcoded list, no `domains.json` file, and no fixed count — the table starts empty and populates as the user ingests material.

Population sources, in order: subfolder names within `prism/prism-inbox/` (auto-seeded as new rows during ingestion), prompt context the user supplies during an ingest call (honoured as direction), and conversational refinement via `prism_core_domains_set` driven by the `prism-companion` skill or by clicking domain nodes in the Graph Explorer. The classifier, the graph, and the explorer all read from the table.

The previous design copied a `domains-starter.json` template into the workspace at first launch and treated it as authoritative. That design imposed Simon-specific (or otherwise opinionated) categories on every install and was deleted in the v2 remediation.

---

## What Ships and What Stays Personal

### Ships with the plugin (the engine)

Everything content-independent. The database engine (brain.py). The MCP server. The search system. The classifier. The inbox pipeline. The graph explorer template (empty, ready to populate). The bootstrap. The extension discovery system.

These work regardless of who uses them or what they ingest.

### Does NOT ship (your content)

Everything that makes this brain yours. Your database contents. Your axioms. Your source files. Your domain configurations. Your graph data. Your extensions.

Each user builds their own brain. The plugin provides the empty container and the tools to fill it. The content is yours.

---

## The Architecture, Visually

```
YOU
 |
 | drop files into prism/prism-inbox/
 v
[Inbox Pipeline]
 |
 | chunk, embed, classify
 | LLM concept extraction (skill-driven, post-ingestion)
 | wire graph
 v
[prism-brain.db]  <-- single source of truth
 |
 | auto-generates on every change
 v
[Outputs]
 ├── prism-graph-explorer.html  (interactive map, also renders in Cowork canvas)
 └── prism-graph.json           (graph data, projected from database)

[MCP Server]  <-- how Cowork talks to the engine
 |
 | structured requests and responses
 | exposes tools (prism_core_search, prism_core_ingest, ...) for Claude to call
 | discovers and registers extensions
 v
[brain.py]  <-- the engine, does all the work
 |
 | reads/writes
 v
[prism-brain.db]

[Extensions]  (optional, drop-in)
 ├── 51folds/    (prediction system)
 └── future/     (whatever you build)

[User-authored]
 └── prism-axioms.md   (your intellectual framework, not generated)
```

---

## Settled Decisions

These questions have been resolved. The rationale is recorded here to prevent re-litigation.

1. **LLM concept extraction during ingestion: aggressive with deferred review.** Claude creates concept nodes and proposes edges during ingestion without stopping to ask. Review happens later, opt-in: in the Graph Explorer when the user inspects the new clusters, or conversationally through the `prism-companion` skill when the user engages Claude on the recent ingestions. This scales — a conservative "approve each one" loop would make ingesting 30 sources during setup unbearable. Deferring review keeps ingestion frictionless without losing user oversight.

2. **Brain search invocation: Claude judges per prompt.** MCP cannot intercept prompts. The MCP server exposes `prism_core_search` as a tool; Claude calls it when the question would benefit from the user's reading. The decision logic lives in the `prism-bootstrap` skill: working memory first, brain search for depth questions, direct file read for structural questions, training knowledge for general background. There is no auto-fire on every prompt. If the user wants the brain consulted explicitly, they can phrase the question that way ("what does my brain say about X?") and Claude will call the tool.

3. **Graph explorer click-to-Claude: button-triggered, not automatic.** Clicking a node shows the detail panel with metadata and connections. Below the panel, two explicit buttons: "Summarise" and "Ask a question." No auto-summary. Auto-summaries on large graphs would fire Claude calls on every misclick. The buttons give the user control over when to invoke Claude and when to just browse.

4. **Extension API surface: restricted set.** Extensions can call: search, add_node, add_edge, get_graph_data, export_graph, stats. They cannot call: ingest (which would bypass the inbox pipeline), delete operations (which could corrupt the graph), or direct SQL (which could break the schema). If a future extension needs more access, the restricted set can be expanded. Starting restricted prevents coupling that is hard to undo.

5. **Remote/cloud storage: not in v1.** SQLite for the plugin. The MCP server abstracts the storage layer, so the interface does not change if the backend swaps to Postgres later. Designing the abstraction layer now is unnecessary complexity. When a user hits SQLite limits (which requires an extraordinary volume of material), migration to cloud Postgres is a straightforward swap behind the existing MCP interface. Build it when someone needs it.

---

## Companion: [PLUGIN-NEW-USER-EXPERIENCE.md](PLUGIN-NEW-USER-EXPERIENCE.md)

That document covers what happens when someone installs the plugin for the first time: what they see, what they do, how the brain goes from empty to useful.
