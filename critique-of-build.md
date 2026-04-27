# PRISM Plugin — Full Audit

## What it is

A Cowork plugin that turns a workspace folder into a personal knowledge base. You drop files in `prism/prism-inbox/`, it chunks them, embeds them, classifies them into domains, and wires them into a SQLite-backed knowledge graph. You search your own thinking through hybrid BM25 + semantic retrieval, blended via Reciprocal Rank Fusion. You explore the graph via a D3.js force-directed map. You author an `prism-axioms.md` file — your prism, supporting frameworks, and cross-domain bridges — that a Socratic companion skill is supposed to pressure-test as you read more.

Underneath it's a two-language sandwich: a Node MCP server (`server/index.js`, 492 LOC) spawns a Python bridge (`engine/bridge.py`, 278 LOC) for every tool call, and the bridge imports the real engine (`engine/brain.py`, 2,361 LOC). SQLite is the single source of truth. Everything else — `prism-graph.json`, `prism-graph-explorer.html`, the source registry — is a projection regenerated after every mutation. It ships a reference brain of 32 public-domain texts (Smith, Kant, Locke, Darwin, Machiavelli, etc.) with 93 chunks, 66 nodes, 128 edges, and a pre-written `AXIOMS-reference.md`. A working 51Folds prediction extension demonstrates the drop-in extension architecture with a deliberately restricted API surface.

## What it's trying to be

Two things at once, and this tension shapes every judgment below.

First: a distributable product — "PRISM" — for anyone who reads seriously and wants to build a structured second brain. That's the README, the templates, the generic `domains-starter.json` with ten pleasant domains, the reference brain as pedagogical contrast, the prism-starter onboarding conversation. Positioned against Roam/Obsidian/Notion but local-first and LLM-native.

Second: your personal thinking system. The original working title was "Frameworks of Understanding." The code gives it away. The (supposedly generic) `prism-bootstrap/SKILL.md` is actually your private "Great Inversion" master thesis, four hypotheses about machine population and bifurcated inflation, and the full Simon-specific operating axioms — not a generic bootstrap. The 51Folds extension is your prediction system. The philosophy (a prism is a debatable claim, frameworks must be cited, bridges are where original thinking lives) is specifically yours.

## Verdict on execution

**The engineering is solid.** 166 tests, all passing on a fresh run (I ran them). Clean separation of deterministic code (engine) from judgment (protocol). Transaction-per-source with savepoint rollback in ingestion. Content-hash dedup. Configurable workspace root. TF-IDF fallback when sentence-transformers can't download the model behind a proxy — a real, thoughtful concession to the Cowork VM reality. The extension architecture with its restricted API (search, add_node, add_edge, get_graph_data, export_graph, stats) is exactly the right starting surface. The docs are better than most plugins will ever have: PROTOCOL, HOW-IT-WORKS, NEW-USER-EXPERIENCE, and BUILD-PLAN all coherent and cross-referenced.

**But there are gaps between what the docs promise and what the code does.**

1. The `skills/prism-bootstrap/SKILL.md` file in the source tree is a **corrupted ZIP archive** containing, when unpacked, your old Simon-specific `brain-bootstrap` skill (the Great Inversion axioms). The manifest says it's generic session initialisation. An installer reading this file will either blow up or leak your private thesis into a stranger's brain. The `dist/` tarball has a different, correctly-named skill — which tells me the live source and the last packaged build are out of sync.

2. The docs repeatedly claim the MCP server "runs a prism_core_search automatically on every user prompt" and "watches the inbox folder and auto-triggers ingestion." **Neither is wired up.** MCP is a tool-calling protocol; the server can't intercept prompts. There's no fs.watch on the inbox. Auto-search and drop-and-forget are the two selling points of the magic experience, and in practice Claude has to be reminded to call `prism_core_search` and the user has to say "ingest my new files." This doesn't break anything, but the "invisible brain always present" story in the user docs is aspirational, not implemented.

3. Subprocess-per-call: every MCP tool invocation spawns a fresh `python3 bridge.py`, which re-imports `brain.py`, re-initialises the embedder, and re-restores the database from workspace. Fine for `prism_core_stats`. Painful for a "search on every prompt" workflow if that were actually happening — hundreds of milliseconds of cold start on every call, plus the model reinit tax when sentence-transformers is available. A long-running Python daemon with a socket would be the right move if you wanted latency.

4. Concept extraction is a manual multi-tool loop (`extract_concepts` → `propose_concept` → `review_proposals` → `accept/reject_proposal`), which is fine, but the `prism-starter` skill doesn't walk the user through it. Phase 3 of the onboarding mentions "Claude extracts key concepts" as if it's automatic. In practice Claude has to know to orchestrate four tool calls per source, and the skill doesn't say so.

5. Small things: `manifest.json` lists 8 tools; the server registers 14 (concept proposal tools aren't declared). `node_modules/` is committed. The 51Folds extension hardcodes `group_id=11` for predictions, but the generic starter only has domains 1–10. The bootstrap.sh isn't actually invoked by the server — it's assumed the user has run it themselves, but the first-run story says they shouldn't need to.

## Will it be successful?

Split the question.

**As your personal brain — high probability of success.** The methodology is yours, the reference material mirrors your reading, the prism/domains/bridges structure is how you already think, and the 51Folds extension maps a workflow you actually run. The engine is genuinely good at what a personal knowledge base should do: cheap to run, local, no cloud dependency, graph you can see, axioms you can cite. You'll get value from the first session you use it in earnest. Most of the gaps above (corrupted SKILL.md, auto-search not wired up, subprocess overhead) don't bite hard for a single user who tolerates rough edges.

**As a distributable product — uncertain, and the odds are not in its favour.** Personal knowledge management is a graveyard. Roam, Obsidian, Logseq, Mem, Reflect, Tana, Notion — mature UIs, big communities, plugin ecosystems, mobile apps. PRISM is a Cowork plugin that requires Node 18+, Python 3.8+, a workspace folder, comfort with markdown, and — this is the hard part — a willingness to write a one-sentence prism and defend it against Claude's pushback before you can even ingest a file. That's a very specific audience: the contrarian essayist who wants their reading structured and doesn't mind a CLI-shaped tool. It's a real audience, but it's thousands of people, not millions. The opinionated onboarding ("we will not advance until your prism has an edge") is both the thing that makes it interesting and the thing that will make most installers quit at Phase 0.

The ceiling is "cult tool for a specific kind of thinker." The floor is "my personal system that I open-sourced and two friends use." Neither outcome is bad. Neither is venture-scale either, and the tool doesn't seem to be pretending it is.

**Odds if I had to number them:** 95% this is successful as your personal system. 40% it gets 100+ genuine users outside your circle within six months of release. 5% it achieves meaningful adoption (thousands of users). The gap between the first and second figure is almost entirely friction — the corrupted bootstrap skill, the unimplemented auto-search magic, the Phase 0 prism gate, the missing mobile story. Each of those is fixable; none of them is the reason PKM tools usually fail, which is that humans don't actually want to structure their thinking that much.

## The one structural observation worth making

The most valuable thing in the repo isn't the engine. The engine is solid but ordinary RAG-plus-graph. The valuable thing is the **methodology encoded in the three skills and `prism-axioms.md`** — prism → domains → cited frameworks → named cross-domain bridges, with a Socratic companion that refuses to let you get away with platitudes. That's the product. The code is the vehicle.

If you want to sharpen the bet: lean harder into the methodology as the differentiator. Obsidian will never ship a skill that tells you your prism is a truism and refuses to advance. That's the moat. Everything else is catchup to tools with three-year head starts.