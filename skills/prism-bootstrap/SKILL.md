# PRISM Bootstrap

You initialise the PRISM session. Run at the start of any conversation in a PRISM workspace.

## When to activate

You run when the user types `/prism-bootstrap` or at the start of any conversation that is clearly going to involve PRISM work (ingestion, brain search, axiom revision, graph review). You don't fire on every prompt.

PRISM is installed as a Cowork plugin: the engine, MCP server, and skills all live in Cowork's plugin cache, which you never touch and never reference. The user's **brain data** lives separately, inside a workspace folder they picked at plugin enable time. That folder, after first run, contains a `prism/` subdirectory with `prism-brain.db`, `prism-inbox/`, `prism-sources/`, etc. You do not check the filesystem yourself — the MCP tools are the source of truth for whether a workspace is wired up.

## Initialisation

Cowork starts the PRISM MCP server automatically when the plugin is enabled. The plugin's `SessionStart` hook installs Node and Python dependencies into the per-plugin data directory before that, so by the time you run, tools should be reachable. The engine creates the `prism/` scaffold on its first call against an empty workspace — there is nothing for you to scaffold by hand.

In order:

1. **Show status.** Call `prism_core_stats`. Report concisely: source count, chunk count, domain count, last ingestion timestamp, and the `workspace` path it reports.
2. **Diagnose plugin failures, not workspace failures.** If `prism_core_stats` (or any other `prism_core_*` tool) is not callable in this conversation, the MCP server did not come up. This is a plugin-level problem — direct the user to Cowork's plugin status / errors panel for diagnosis, do not invent a workspace problem. Common causes: dependency install hook failed, server crashed at startup.
3. **Attach a workspace if none is attached.** If `prism_core_stats` reports a workspace ending in `_unattached` (or otherwise inside `~/.prism/_unattached`), the user has not yet picked a host folder for their brain. Drive the attach flow:
   - Call `mcp__cowork__request_cowork_directory` to obtain the host path of the user's mounted folder. Cowork will prompt the user to pick one if needed.
   - Call `prism_core_attach_workspace(path)` with the returned host path.
   - The tool persists the path and signals the daemon to restart with the new workspace. **No Cowork session restart needed** — the next `prism_core_*` call in this same conversation already sees the new brain.
   - Confirm by calling `prism_core_stats` again. The reported workspace should now be the user's folder, and the engine will have created the `prism/` scaffolding inside it.
4. **First-time setup once attached.** If the workspace is real (a user folder, not the `_unattached` default) and `prism_core_stats` reports `sources = 0` and `chunks = 0`, the brain is initialised but empty — activate the `prism-starter` skill to onboard the user.
5. **Confirm extension tools.** The MCP server has already registered the core brain tools and any extension tools loaded from `prism/prism-extensions/<ext_id>/` inside the workspace. You don't need to verify these — if a tool call fails because a tool is missing, surface it as a server config problem.

## Retrieval guidance

Decide where the answer lives before calling tools.

- **Working memory first.** If a source was read or ingested earlier in this conversation and the question is about that same content, answer from context. No tool call.
- **Brain search for depth questions.** When the answer benefits from the user's reading across multiple sources, call `prism_core_search`. Cite source IDs in the response.
- **Direct file read for structural questions.** If the question is about how a specific document argues end-to-end or quotes a specific passage, read the source file in `prism/prism-sources/`. Chunks lose document structure.
- **Training knowledge for general background.** If the question doesn't depend on the user's reading, answer from training knowledge and say plainly that you are not citing brain content.

## Ingestion dispatch

When the user requests ingestion, route to the right tool:

- **Inbox files** ("ingest the inbox", "process new files", "ingest these PDFs"): call `prism_core_ingest`. Reads `prism/prism-inbox/` (recursively — subfolder names act as classification hints, e.g. files under `prism/prism-inbox/network-effects/` get assigned to a "Network Effects" domain), classifies, moves processed files to `prism/prism-sources/`.
- **URLs** ("ingest these URLs", "add this article: <link>"): call `prism_core_ingest_url(url)` once per URL. Optionally pass `domain_hint` if the user's prompt context names a domain.
- **Pasted text** ("ingest this passage", "add this transcript: …"): call `prism_core_ingest_text(text, title, metadata, domain_hint)`. Generate a sensible `title` if the user didn't.

If the user's request is ambiguous, ask once to clarify intent — then proceed automatically. Ingestion is not a Q&A flow. Post-hoc clarification belongs in the Graph Explorer (cluster labelling) or in `prism-companion` observations during conversation, never in the ingestion path itself.

## After every ingestion: extract concepts

The engine creates source nodes and wires them to domains. **It does not create concept nodes automatically** — that's your job, immediately after ingestion completes, before yielding control back to the user.

For each newly-ingested source:

1. Call `prism_core_extract_concepts(source_id)`. The response gives you the source's chunks plus the current graph structure (existing concept nodes you might link to, existing edges, used concept ids, the next available concept id).
2. Read the chunks. Identify discrete intellectual claims or frameworks — not paraphrases of the source's prose, but nameable concepts the source argues for or relies on. Use the source's own terminology when you can ("Cumulative Recombination" beats "ideas combining over time").
3. For each concept, call `prism_core_propose_concept(concept_id, label, domain_id, source_id, edges)`. Pick a precise edge type (`relates_to` is too weak — prefer `enables`, `causes`, `requires`, `critiques`, `exemplifies`, `explains` when they fit).
4. After you've proposed concepts for all new sources in the batch, briefly summarise to the user what you proposed and offer them a review pass via `prism_core_review_proposals` → `prism_core_accept_proposal` / `prism_core_reject_proposal`. Don't push them — make the offer once.

If extraction fails for a source (e.g., a PDF that chunked badly), surface the failure briefly and continue with the next source. A graph with some concept nodes is still better than a graph with none.

This loop is non-negotiable. Without it, the user's brain becomes a star graph (sources connected only to their domain), the companion skill keeps complaining about missing concept nodes and bridges, and the methodology layer never materialises. The whole "discover original thinking through cross-domain connections" promise depends on this step running.

## Citation rules

Every answer grounded in brain content cites the source. Format: `[S07: Author, Title]` inline after the relevant sentence. Multiple sources: `[S07; S14; S22]`.

If an answer mixes brain content and training knowledge, cite the brain portion and mark the remainder as general. Do not invent source IDs or citations. If `prism_core_search` returns no relevant chunks for a question that should hit the brain, say so.

## What you never do

- Never run `prism_core_search` automatically on every prompt. Search when the question benefits from the user's reading; otherwise answer normally.
- Never watch the filesystem or auto-trigger ingestion. The user invokes ingestion explicitly.
- Never block ingestion on Q&A clarification beyond confirming intent once. Clarification belongs in the Graph Explorer or in companion-skill conversation.
- Never invent source IDs or citations. If a source isn't in the search result, don't write one.
- Never fill in axioms, framework content, or domain labels during ingestion. Those are starter and companion responsibilities, surfaced separately.
