# PRISM

Every question you ask Claude is answered against the full depth of what you have read.

PRISM is your *frameworks of understanding* — a personal, structured, searchable extension of memory that grows richer every session. The name reflects the methodology: a prism refracts incoming material through your own framework into structured, connected thinking. The value is universal: you do not need to articulate that framework upfront. Anyone who reads anything seriously stands to gain.

**The benefit.** Claude does not just answer from training knowledge or the gist of what you've read. It pulls the actual passages — Chen on atomic networks, Ridley on cumulative recombination, Thiel on monopoly thresholds, plus everything else in your library on the topic — and answers using all of it. You cannot hold that depth in working memory. PRISM holds it for you.

**Three ways to feed it.** The Cowork prompt is the universal entry point.

- **Files.** Drop articles, PDFs, notes, essays into the `prism/prism-inbox/` folder. Subfolder names are honoured as classification hints.
- **URLs.** Paste links into the prompt: "ingest these: [URLs]" — Claude fetches, extracts, ingests.
- **Pasted text.** Paste a transcript or excerpt into the prompt: "ingest this passage: ..." — the content becomes a source.

In every case you tell Claude to ingest; the engine chunks, embeds, classifies, wires into the graph. Ingestion never blocks for Q&A. Clarification happens later — in the Graph Explorer (cluster-level naming) or in conversation with Claude.

**Enrichment over time.** As your library grows, patterns emerge. Through directed conversation, Claude helps you name your recurring claims (axioms), notice your domains of interest, and surface the cross-domain bridges where original thinking lives. The plug-in is the scribe; you author through dialogue, edits, and rejections.

Your data stays on your machine. No cloud. No account. You own it.

## Naming and namespacing

Everything PRISM creates lives under a single container at the workspace root: `prism/`. Children inside it are prefixed `prism-`. This keeps the workspace folder yours — PRISM never scatters loose files at the root, and the layout is identical on macOS, Linux, and Windows.

| Resource | Pattern | Example |
|---|---|---|
| Workspace container | `prism/` | — |
| Files & folders inside container | `prism-<name>` | `prism/prism-brain.db`, `prism/prism-inbox/` |
| Skills | `prism-<name>` | `prism-bootstrap` |
| Core MCP tools | `prism_core_<tool>` | `prism_core_search` |
| Extension MCP tools | `prism_<ext>_<tool>` | `prism_folds_refine_thesis` |
| Env vars | `PRISM_<NAME>` | `PRISM_WORKSPACE` |
| Extension env vars | `PRISM_<EXT>_<NAME>` | `PRISM_FOLDS_API_TOKEN` |

Extension authors declare bare tool names; the loader rewrites them to `prism_<ext>_<tool>` and refuses any name that would collide with a core tool or another extension. Naming is enforced in code, not advised in docs.

## Installation

PRISM is a **Cowork** plugin. Cowork is the local agent runtime that ships with Claude Desktop — PRISM does not run in the standalone Claude Code CLI, and is not architected to.

There are two supported install paths today: marketplace install (everyday users) and local-clone install (developers iterating on the plugin). Both end in the same place — PRISM loaded as a Cowork plugin — and both auto-install Python and Node dependencies into the Cowork VM on each session start.

### Path 1 — From a marketplace (recommended)

This repository is itself a one-plugin marketplace. To install in Cowork:

1. Open Claude Desktop → Cowork.
2. **Customize → Browse plugins → Add custom marketplace** (or the equivalent UI affordance for adding a non-official marketplace).
3. Paste the GitHub URL: `https://github.com/cyclomaticsegal/prism-plugin`
4. Find **PRISM** in the listing and install it.

Cowork derives the marketplace identifier from the GitHub repo path, so the installed plugin appears as `prism@prism-plugin` (not `prism@cyclomaticsegal` — the `name` in `marketplace.json` is overridden by the source path).

> The Claude Code CLI form `/plugin marketplace add cyclomaticsegal/prism-plugin` followed by `/plugin install prism@prism-plugin` works on the standalone CLI but is not currently accepted as a slash command inside Cowork (Cowork resolves `/plugin` as a skill name and reports "unknown skill"). Use the UI flow.

Your **workspace folder is whichever folder Cowork is rooted in for the current session.** PRISM does not ask you to pick one — it uses the session's working directory and creates a `prism/` subfolder inside it on first ingest. To start a new brain, open Cowork against a different folder; to come back to an existing one, open Cowork against its folder. One plugin, N independent brains.

To begin using PRISM, drop something into `prism/prism-inbox/` and ask Claude to ingest it.

### Path 2 — From the Anthropic plugin marketplace

Not yet published. When it lands, install will be one click from Cowork's plugin browser; this section will be replaced with the exact flow.

### Path 3 — From a local clone (development)

For working on the plugin itself, or for running an unreleased branch:

```bash
git clone https://github.com/cyclomaticsegal/prism-plugin.git
```

Then in Cowork's plugin UI, use the same **Add custom marketplace** affordance and paste the absolute path to the local checkout instead of a GitHub URL. The plugin appears under the same `prism@prism-plugin` identifier (Cowork uses the directory basename as the marketplace name when adding from a local path).

Pull new changes with `git pull` and refresh the marketplace from the same UI panel.

### Dependencies — auto vs manual

On every Cowork session start, the plugin runs a hook (`scripts/install-deps.sh`) that diffs `server/package.json` and `engine/requirements.txt` against the cached copies in `${CLAUDE_PLUGIN_DATA}` and installs anything missing or changed. Node modules land in `${CLAUDE_PLUGIN_DATA}/node_modules`; Python lives in `${CLAUDE_PLUGIN_DATA}/venv`. Nothing is installed into your host environment — everything stays inside the Cowork VM and the plugin's own data directory.

To opt out (offline installs, locked dependency versions, debugging), set:

```text
PRISM_AUTO_BOOTSTRAP=0
```

…and install the dependencies yourself before starting the session.

The exact `${CLAUDE_PLUGIN_DATA}` path Cowork uses for PRISM varies by install method, so locate it on disk first:

```bash
# macOS — find the data dir Cowork chose for PRISM (usually ends in 'prism-inline' or 'prism-prism-plugin')
find "$HOME/Library/Application Support/Claude" -type d -name 'prism-*' 2>/dev/null | grep plugins/data
```

Then install both stacks into that directory:

```bash
PRISM_DATA=/path/from/the/find/above

# Python — requires Python 3.8+
python3 -m venv "$PRISM_DATA/venv"
"$PRISM_DATA/venv/bin/pip" install -r <plugin-root>/engine/requirements.txt

# Node — requires Node.js 18+
cp <plugin-root>/server/package.json "$PRISM_DATA/"
(cd "$PRISM_DATA" && npm install)

# ESM resolution shim: Node's ESM loader doesn't honour NODE_PATH, so the
# server expects node_modules next to itself. Symlink, don't copy.
ln -sfn "$PRISM_DATA/node_modules" "<plugin-root>/server/node_modules"
```

Replace `<plugin-root>` with the directory containing `server/` and `engine/` — the auto-bootstrap hook normally derives this from `${CLAUDE_PLUGIN_ROOT}`. Note the trailing `ln -sfn` step: without it the MCP server crashes at startup with `Cannot find package '@modelcontextprotocol/sdk'`, even though the package is installed correctly.

### What happens on first launch

The plugin detects an empty workspace and initialises a single `prism/` container with everything inside:

- **`prism/prism-brain.db`** — your database. Tables for chunks, embeddings, nodes, edges, sources, predictions, domains, and axioms. Empty and ready.
- **`prism/prism-inbox/`** — drop folder for new material
- **`prism/prism-sources/`** — read-only archive of ingested files (the database owns the content; files here are reference copies)
- **`prism/prism-extensions/`** — optional drop-in integrations
- **`prism/prism-graph-explorer.html`** — interactive map, regenerated from `prism-brain.db` after every change
- **`prism/prism-axioms.md`** — read-only projection of your axioms table, regenerated whenever an axiom is added or revised. Edit through Claude, not by hand.

You see a brief welcome and one suggestion: drop something into `prism/prism-inbox/` and ask Claude to ingest it. Domains and axioms emerge through ingestion and conversation — there is no setup form to fill out, no prism to articulate up front.

### Migration from the pre-2.0 layout

If you upgraded from a workspace built by an earlier PRISM version (loose `brain.db`, `AXIOMS.md`, `GRAPH.json`, `inbox/`, `sources/`, `extensions/` at the workspace root), the bootstrap performs a one-time migration on first launch: every legacy file or folder is moved into `prism/` and renamed with the `prism-` prefix. Source-path metadata in the database is rewritten in the same step. The migration is idempotent and logs every move it makes.

### What if something goes wrong

| Situation | What happens |
|-----------|--------------|
| You select a folder that already has a `prism/prism-brain.db` | Not a first install — PRISM loads your existing brain. No data is lost or overwritten. |
| You select a folder without write permissions | PRISM tells you: "PRISM needs write access to this folder to create your brain. Please select a folder you have write access to." |
| Projections are missing or out of sync (`prism-graph-explorer.html`, `prism-axioms.md`, `prism-graph.json`) | PRISM regenerates them from `prism-brain.db`. The database is the truth; projections rebuild from it. |
| Auto-bootstrap fails (the SessionStart hook reports `npm install failed` or `pip install failed`) | The hook removes its cached marker so the next session retries. You can run `bash scripts/install-deps.sh` manually with `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` set, or fall back to manual dependencies above. |

### Running tests and the reference brain

The dev-loop commands previously documented under "For development" — running the test suite, building the reference brain — work the same way regardless of how the plugin is installed:

```bash
# Run tests
python3 -m pytest tests/ -v

# Build the reference brain (optional — pre-built DB not committed)
python3 reference-brain/build.py
```

## Architecture

```
Cowork/Claude Desktop
    |
    | MCP (stdio)
    v
server/index.js          MCP server — registers tools, discovers extensions
    |
    | JSON over stdin/stdout
    v
engine/bridge.py         Python-Node bridge — translates commands
    |
    | direct function calls
    v
engine/brain.py          Core engine — ingest, search, graph, classify, export
    |
    | SQLite
    v
prism/prism-brain.db    Single source of truth — graph + chunks + embeddings
```

`prism-brain.db` is the single source of truth. Everything else is either an input (files in `prism/prism-inbox/`, content pasted via the Cowork prompt) or a projection (`prism-graph-explorer.html`, `prism-graph.json`, `prism-axioms.md`). Inputs flow into the database; projections regenerate from it.

## MCP Tools

22 core tools (extension tools live in their own manifests).

| Tool | Description |
|------|-------------|
| `prism_core_search` | Hybrid search (keyword + semantic). Optional `include_reference` for parallel reference-brain search |
| `prism_core_ingest` | Process `prism/prism-inbox/` — chunk, embed, classify, wire graph. Subfolder names are honoured as classification hints |
| `prism_core_inbox` | Alias for `prism_core_ingest` |
| `prism_core_ingest_url` | Fetch a URL, extract text, ingest as a new source |
| `prism_core_ingest_text` | Ingest pasted text as a new source |
| `prism_core_reingest` | Re-read a source file in `prism/prism-sources/` and regenerate chunks/embeddings |
| `prism_core_source_delete` | Remove a source from the brain (file in `prism-sources/` is left untouched) |
| `prism_core_tag` | Manual domain classification correction |
| `prism_core_graph` | Query subgraph within N hops of a node |
| `prism_core_stats` | Database statistics |
| `prism_core_export` | Regenerate graph explorer and `prism-graph.json` |
| `prism_core_index` | Full source registry |
| `prism_core_domains_get` | Read the `domains` table (source of truth for classification) |
| `prism_core_domains_set` | Create or update a domain row |
| `prism_core_axioms_get` | Read the `axioms` table (active axioms) |
| `prism_core_axioms_revise` | Add or revise an axiom (writes a new row, marks predecessor superseded, regenerates `prism-axioms.md`) |
| `prism_core_axioms_history` | Full revision history for an axiom key |
| `prism_core_extract_concepts` | Get extraction context for LLM concept identification |
| `prism_core_propose_concept` | Create a proposed concept node |
| `prism_core_review_proposals` | List pending concept proposals |
| `prism_core_accept_proposal` | Accept a proposed concept |
| `prism_core_reject_proposal` | Reject a proposed concept (logs correction) |

## Project Structure

```
prism-plugin/
├── .claude-plugin/
│   ├── plugin.json                  Plugin manifest (Claude Code schema — name, version, metadata)
│   └── marketplace.json             One-plugin marketplace listing (`cyclomaticsegal`)
├── .mcp.json                        MCP server registration (PRISM as a stdio server)
├── hooks/
│   └── hooks.json                   SessionStart hook → scripts/install-deps.sh
├── scripts/
│   ├── install-deps.sh              Idempotent dep install into ${CLAUDE_PLUGIN_DATA}
│   └── package.sh                   Tarball packaging (legacy distribution path)
├── server/
│   ├── index.js                     MCP server (registers tools, discovers extensions)
│   └── package.json
├── engine/
│   ├── brain.py                     Core engine (~2,800 lines): ingest, search, graph, classify,
│   │                                domains/axioms CRUD, projection regeneration
│   ├── bridge.py                    Python-Node bridge (JSON over stdin/stdout)
│   ├── bootstrap.sh                 Standalone Python-only deps script (kept for manual use)
│   └── requirements.txt
├── templates/
│   └── graph-explorer-template.html D3.js explorer shell, populated from prism-brain.db on every mutation
├── skills/
│   ├── prism-bootstrap/SKILL.md     Session init: retrieval guidance, ingestion dispatch, citation rules
│   ├── prism-starter/SKILL.md       New-user onboarding (ingest-first, no blocking Q&A)
│   └── prism-companion/SKILL.md     Ongoing coaching: citation tests, boundary discipline, observation
├── reference-brain/                 Pre-built demo brain — build inputs, not user-facing config
│   ├── sources/                     32 public-domain works (pre-1929)
│   ├── domains-reference.json       *Build input only* — read by build.py and written into the
│   │                                pre-built DB's `domains` table. Not read at runtime.
│   ├── AXIOMS-reference.md          *Build input only* — example axiom prose authored by hand
│   │                                for the reference brain. The runtime prism-axioms.md projection
│   │                                in a user workspace is regenerated from their own
│   │                                axioms table; this file is a frozen demo, not a template.
│   └── build.py                     Builds brain-reference.db via upsert_domain + revise_axiom
├── extensions/
│   └── 51folds/                     Lean v1 prediction-engine integration (id: "folds")
│       ├── client.js                HTTP wrappers (api.51folds.ai + app.51folds.ai)
│       ├── tools.js                 4 tools: refine_thesis, create, status, ingest_results
│       │                            (registered as prism_folds_<name>)
│       ├── manifest.json
│       └── .env.example             Bearer-token template (.env is gitignored)
├── tests/                           Pytest suite — run with `python3 -m pytest tests/ -v`
│   ├── test_engine.py               Engine: ingest, search, graph, tag, stats
│   ├── test_classifier.py           Domain classification, table-backed loading
│   ├── test_server.py               Bridge commands and subprocess mode
│   ├── test_bootstrap.py            First-run, idempotency, legacy-layout migration
│   ├── test_extraction.py           Concept proposals lifecycle
│   ├── test_reference.py            Reference brain artifacts and search
│   ├── test_extensions.py           Extension discovery, naming enforcement, restricted API
│   ├── test_daemon.py               Persistent daemon mode (NDJSON loop, respawn, fallback)
│   ├── test_axioms_lifecycle.py     Supersession chain, projection regen, citation check
│   ├── test_source_lifecycle.py     Reingest preserves wiring; delete cascades cleanly
│   ├── test_ingestion_modes.py      ingest_text, ingest_url (mocked HTTP), folder hints
│   ├── test_packaging.py            Regression test for the UTF-8 SKILL.md guard
│   └── fixtures/                    Test data (5 sources, test domains)
└── docs/
    ├── PLUGIN-BUILD-PLAN.md         Build plan and artifact inventory
    ├── PLUGIN-HOW-IT-WORKS.md       Architecture reference
    ├── PLUGIN-NEW-USER-EXPERIENCE.md User journey specification
    └── PROTOCOL.md                  Operational rules for Claude's judgment layer
```

## Creating Extensions

Extensions are drop-in folders in the workspace's `prism/prism-extensions/` directory. Tool names declared in the manifest are bare identifiers — the loader stamps them as `prism_<ext_id>_<tool>` at registration time. The extension id must match `^[a-z][a-z0-9]*$`.

### Manifest

```json
{
  "name": "myext",
  "version": "1.0.0",
  "description": "What it does",
  "tools": [
    { "name": "my_tool", "handler": "myHandler" }
  ],
  "node_types": ["custom_type"],
  "edge_types": ["custom_edge"],
  "requires": ["search", "addNode"]
}
```

The tool above is registered as `prism_myext_my_tool`. The loader refuses to register it if the name collides with a core tool or with another already-loaded extension.

### Tools

```javascript
// tools.js
export default function register(brainAPI, z) {
  return [
    {
      name: "my_tool",                      // bare; loader stamps prism_myext_my_tool
      description: "What this tool does",
      inputSchema: z.object({
        param: z.string().describe("Parameter description"),
      }),
      handler: async ({ param }, api) => {
        const results = await api.search(param, 3);
        await api.addNode("N01", "My Node", "concept", 1);
        return { status: "ok", results };
      },
    },
  ];
}
```

### Restricted API

Extensions receive a restricted brain API with these methods only:

| Method | Description |
|--------|-------------|
| `search(query, top_k)` | Hybrid search |
| `addNode(id, label, type, group_id, metadata)` | Add/update a graph node |
| `addEdge(source, target, type, label)` | Add a graph edge |
| `getGraphData()` | Full graph as JSON |
| `exportGraph()` | Regenerate explorer files |
| `stats()` | Database statistics |

Install by dropping the folder into `prism/prism-extensions/`. Remove by deleting it. Update by replacing it. The core engine never depends on extensions.

### Environment variables

Extensions that need configuration must use `PRISM_<EXT_ID>_<NAME>` env vars (e.g. `PRISM_FOLDS_API_TOKEN`). This keeps every PRISM-related env var rooted at the `PRISM_` namespace and tied to its extension by id.

## Development

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run a specific test file
python3 -m pytest tests/test_engine.py -v

# Start the MCP server (for debugging)
node server/index.js

# Build the reference brain
python3 reference-brain/build.py

# Engine CLI (standalone debugging)
python3 engine/brain.py search "your query"
python3 engine/brain.py stats
python3 engine/brain.py registry
```

## Design Documents

- [How It Works](docs/PLUGIN-HOW-IT-WORKS.md) — architecture reference
- [New User Experience](docs/PLUGIN-NEW-USER-EXPERIENCE.md) — user journey spec
- [Protocol](docs/PROTOCOL.md) — operational rules for Claude's judgment layer
- [Build Plan](docs/PLUGIN-BUILD-PLAN.md) — phased build plan and artifact inventory

## License

MIT License. See [LICENSE](LICENSE).
