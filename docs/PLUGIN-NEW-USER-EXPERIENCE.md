# PRISM — The New User Experience

**Purpose.** Walk through what a new user sees and does from the moment they install PRISM to the point where they have a working, searchable brain. No technical detail. Just the experience.

**Note on naming.** PRISM was extracted from a working personal system ("Frameworks of Understanding"). The methodology's core concept is the prism — the framework through which incoming material gets refracted into structured, connected thinking. But you do not need to articulate that framework upfront; it emerges through use.

**Companion documents:**
- [PLUGIN-HOW-IT-WORKS.md](PLUGIN-HOW-IT-WORKS.md) — how every component works under the hood.
- [PROTOCOL.md](PROTOCOL.md) — architecture protocol; the database is the single source of truth.

---

## Before They Start

The user has installed PRISM into Cowork. They have selected a folder on their computer as their workspace. That folder is empty (or nearly so).

They have no database. No sources. No graph. No axioms. Nothing. The plug-in's job is to take them from here to a functioning second brain — fast — without losing them along the way.

The methodology says the brain emerges from your reading. So the experience leads with the reading, not with theory.

---

## First Launch

The plug-in initialises automatically. The user does not trigger it.

What it creates:

- An empty `prism-brain.db` with the correct schema (graph, chunks, embeddings, **domains**, **axioms**, predictions, meta).
- An `prism/prism-inbox/` folder where the user will drop files.
- A `prism/prism-sources/` folder, where ingested files are archived.
- An `prism/prism-extensions/` folder for optional drop-ins.
- A read-only `prism-axioms.md` projection (regenerated from the `axioms` table — empty for now, with a note explaining how it gets populated).

There is **no** `domains.json`. There is **no** starter axioms file with a "write your prism" prompt. The database starts empty; everything else emerges from what the user feeds in.

The user sees a brief welcome message that says, in substance:

> Welcome to PRISM. The fastest way to feel what this does is to give it something to read.
>
> Three ways to add material:
>
> - **Files.** Drop articles, PDFs, notes, essays into your `prism/prism-inbox/` folder. Subfolder names act as classification hints.
> - **URLs.** Paste links into the prompt: "ingest these: …".
> - **Pasted text.** Paste a transcript or excerpt: "ingest this passage: …".
>
> Once you've added something, ask me to ingest. I'll handle the rest.

That's it. No tutorial. No twelve-step wizard. No prism to write before anything happens.

---

## First Ingestion

The user drops a few files into `prism/prism-inbox/` — or pastes a URL or passage into the prompt — and asks Claude to ingest.

Claude routes the request to the right tool (`prism_core_ingest`, `prism_core_ingest_url`, `prism_core_ingest_text`). The engine:

1. Reads each input.
2. Chunks (~2,000 characters with 256-character overlap).
3. Embeds each chunk (sentence-transformers if available; TF-IDF fallback otherwise).
4. Classifies against the `domains` table. If subfolders are present in `prism/prism-inbox/`, the folder name **seeds a new domain row** if one does not already exist with that label, and the file is assigned there. If the user supplied direction in the prompt ("ingest these as part of my political-economy reading"), Claude honours it as classification context.
5. Writes everything to `prism-brain.db` in a single transaction per source.
6. Moves processed files from `prism/prism-inbox/` to `prism/prism-sources/` (read-only archive — the database owns the content).
7. Regenerates `prism-graph-explorer.html` and `prism-graph.json`.

**Ingestion never blocks for Q&A.** Low-confidence classifications land as-is. The user is not stopped mid-flow to confirm domain assignments. The whole point of B1's remediation is that ingestion stays frictionless.

What the user sees: a confirmation message — "Ingested N sources, classified into [labels], graph regenerated." If they want to look, they open `prism-graph-explorer.html`.

**Volume guidance.** Ten sources is too few — connections won't surface. Thirty to fifty is the minimum for the graph to become interesting. Fifty to a hundred is where insight starts appearing. Include the weird pieces — old essays that don't fit neatly. Cross-domain bridges live there.

---

## Refinement (Opt-in, Two Surfaces)

After ingestion, the user has a populated brain. Refinement is opt-in and lives in two places — never in the ingestion path.

### The Graph Explorer (visual)

Open `prism-graph-explorer.html`. The user sees their reading as an interactive map: nodes coloured by domain, edges by relation, clusters forming naturally.

Click a domain node. The detail panel shows the current label, keywords, and member count, plus two buttons:

- **Refine label / keywords.** Generates a Cowork-ready prompt like "Refine domain D04 (current label: 'Network Effects', 12 members). Read the cluster members via prism_core_index, propose a sharper label and keyword description, then call prism_core_domains_set." The prompt copies to the clipboard. The user pastes it into Cowork; Claude does the work via `prism_core_domains_set`.
- **Review cluster members.** Generates a prompt to review members via `prism_core_index` and surface low-confidence cases for `prism_core_tag` reassignment.

Click any other node. The detail panel offers "Summarise" and "Ask a question" buttons that route to Claude with the node's context preloaded.

### Conversational (Claude as companion)

In any conversation, the `prism-companion` skill surfaces opportunistic observations:

- "Source S07 was classified to D03 with low confidence. Does that fit, or should the domain description be tightened?"
- "Domain D04 has 18 sources but no concept nodes. What ideas are hiding in there?"
- "Concepts C12 and C34 share a theme but live in different domains. Bridge?"

The user can also ask directly: "What's been emerging?" "Do my domains overlap?" "Anything interesting in last week's ingestion?" Companion uses `prism_core_stats`, `prism_core_domains_get`, `prism_core_axioms_get`, `prism_core_graph` to ground its responses.

---

## Axioms — Drafted Through Conversation, Not Hand-Typed

Axioms are claims about how the world works that the user's reading supports. In v1.0 the plug-in asked the user to type them up front. That was wrong; almost no one can articulate their prism on day one. The remediated experience is the inverse: axioms emerge through dialogue, and Claude is the scribe.

The pattern:

1. The user makes a substantive claim in conversation, or asks "what's emerging across what I've read?"
2. Claude searches the brain (`prism_core_search`), surfaces the relevant passages, and drafts an axiom in their voice.
3. Claude applies the coaching discipline carried by `prism-companion`:
   - **Citation tests.** Can the author be named? Is there a brain passage to point to? Does it fit the user's intellectual tone? Would the user recognise it as already-theirs?
   - **Boundary discipline.** Every axiom breaks somewhere. Where does this fail?
   - **Platitude detection.** "Who disagrees with this? If nobody disagrees, it's not a claim."
4. The user accepts, sharpens, or rejects.
5. Claude calls `prism_core_axioms_revise` with the agreed wording, citations, and boundary clause.
6. `prism-axioms.md` regenerates as a read-only projection so the user can browse / grep their axioms in any text editor — but they never edit it by hand.

If they want to revise an existing axiom, the same flow runs and the previous version is marked superseded with `superseded_at` and `superseded_by`. `prism_core_axioms_history` returns the full revision chain.

---

## Living With It

The setup is over from the moment the first source ingests. Now the brain compounds.

**Daily use.** Ask Claude any question. If the answer benefits from the user's reading, Claude calls `prism_core_search` and grounds the response in the user's own corpus, citing source IDs. If the answer is general background, Claude says so plainly and uses training knowledge — never silently passes off training knowledge as brain content.

**Weekly.** Drop new material. Run ingest. The graph grows. Search results improve.

**Monthly.** Talk to Claude about what's emerging. Revise axioms when new material challenges them. Old axioms aren't deleted — they're marked superseded with a dated rationale and a link to the replacement. The brain is a record of thinking, not a clean document that hides the journey.

**Quarterly.** Look at the graph explorer. Where is it dense? Where is it sparse? Are there cross-domain edges, or is every domain an island? The structural shape tells the user where their thinking is strong and where it has gaps.

---

## What "Done" Looks Like

A user with a working PRISM has:

- Thirty or more ingested sources, classified and graph-wired.
- A `domains` table populated through folder hints + conversational refinement (no fixed number — whatever the reading reveals).
- An `axioms` table with cited frameworks and named boundaries, drafted through dialogue.
- A populated, interactive Graph Explorer.
- A weekly ingestion habit.
- A brain that gets more useful with every source added, every search performed, every axiom revised.

There is no "done" milestone — the brain compounds with use.

---

## What the Plug-in Does vs What the User Does

| The plug-in does | The user does |
|---|---|
| Initialises an empty `prism-brain.db` and the workspace folders | Chooses a workspace folder |
| Recognises ingestion intent (file / URL / text) and routes to the right tool | Drops files, pastes URLs, pastes text — and asks Claude to ingest |
| Chunks, embeds, classifies, wires the graph; honours folder hints and prompt context | Optionally organises `prism/prism-inbox/` into subfolders to provide hints |
| Regenerates projections (`prism-graph-explorer.html`, `prism-graph.json`, `prism-axioms.md`) automatically | Opens projections to look at the brain when they want to |
| Surfaces low-confidence cases via the explorer or companion conversation — never blocks ingestion | Refines at their own pace, when ready |
| Drafts axioms from conversation, applying citation and boundary discipline | Accepts, sharpens, or rejects the drafts |
| Searches the database when a question would benefit from the user's reading | Asks the questions that matter to their work |
| Stores everything locally; no cloud, no account | Owns the data outright |

The plug-in is the engine. The user is the thinker. The plug-in never pretends to think for them — it organises, connects, surfaces, and challenges.

---

## The Reference Brain

The plug-in ships with a second, fully populated brain built from public-domain material (Smith, Bacon, Locke, Kant, Madison, Darwin, and 26 others — see `reference-brain/sources/`). It's not the user's brain. It's an example of what a mature brain looks like.

The user can search it. Browse its graph. Read its axioms (the reference axioms are an example of well-formed entries — citation chains, boundary clauses, cross-domain bridges).

The reference brain shows the user what "done" feels like. The contrast between its dense, cross-linked graph and the user's sparse early-stage graph is the pedagogical payload. As the user's brain matures, the reference brain becomes less interesting — that's the signal that the methodology has landed.

---

## The Coaching Layer

Throughout daily use, `prism-companion` runs alongside normal interactions. It does one thing: ask the next question. It reads `prism_core_axioms_get`, `prism_core_stats`, `prism_core_domains_get`, and the graph structure, and it surfaces what's missing, what's thin, what's circular.

Sample observations:

- "You wrote 'quality matters as a principle.' Define quality without circular reference."
- "Your graph has no edges between D02 and D05. Unrelated, or unfound bridge?"
- "You haven't revised an axiom in 60 days. Has nothing challenged your thinking, or have you stopped looking?"
- "S22 just landed. Its claim about Y contradicts the boundary you set on axiom 'X'. Worth a revision?"

The companion doesn't grade. It doesn't tell the user they're doing it wrong. It holds up a mirror and asks the question that makes them look harder. This is the closest the plug-in gets to replicating what a coach does in a consulting engagement, and it's the part of the methodology that v1.0 lost during genericisation and v1.0-remediated restored.

---

*This document describes the post-remediation user experience (v1.0-remediated). The pre-remediation flow — Phase 0 prism articulation, fixed 8-14 domains, hand-edited prism-axioms.md, blocking post-ingestion review — is no longer the design.*
