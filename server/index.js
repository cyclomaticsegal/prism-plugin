#!/usr/bin/env node
/**
 * PRISM MCP Server
 *
 * Wraps brain.py as typed MCP tools for Cowork.
 * Calls the Python engine via engine/bridge.py (JSON over stdin/stdout).
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { spawn } from "node:child_process";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ENGINE_DIR = join(__dirname, "..", "engine");
const BRIDGE = join(ENGINE_DIR, "bridge.py");

const WORKSPACE =
  process.env.PRISM_WORKSPACE || join(__dirname, "..");

// Python interpreter for the engine. The plugin's SessionStart hook
// installs the engine into ${CLAUDE_PLUGIN_DATA}/venv and points
// PRISM_PYTHON at that interpreter via .mcp.json. Outside the plugin
// (manual dev install, tests) the env var is unset and we fall back to
// whatever `python3` resolves to in PATH.
const PYTHON = process.env.PRISM_PYTHON || "python3";

// ---------------------------------------------------------------------------
// Python bridge
//
// Two modes, chosen via PRISM_BRIDGE_MODE:
//
//   daemon (default) — one persistent `bridge.py --daemon` process per
//                      server. Each tool call is an NDJSON request line on
//                      its stdin; responses come back on its stdout. Engine
//                      and sklearn are imported once. ~10-30ms per call
//                      after warm-up vs ~1-2s for spawn mode.
//
//   spawn            — original per-call subprocess. Fresh interpreter for
//                      every tool call. Useful for debugging or as the
//                      automatic fallback when the daemon dies repeatedly.
//
// The daemon mode auto-falls-back to spawn after MAX_RESPAWNS_PER_WINDOW
// process deaths inside RESPAWN_WINDOW_MS — a misbehaving daemon should
// degrade gracefully rather than wedge the server.
// ---------------------------------------------------------------------------

const BRIDGE_MODE_REQUESTED = (process.env.PRISM_BRIDGE_MODE || "daemon").toLowerCase();
const REQUEST_TIMEOUT_MS = Number(process.env.PRISM_REQUEST_TIMEOUT_MS) || 120_000;
const HANDSHAKE_TIMEOUT_MS = Number(process.env.PRISM_HANDSHAKE_TIMEOUT_MS) || 60_000;
const MAX_RESPAWNS_PER_WINDOW = 3;
const RESPAWN_WINDOW_MS = 30_000;

let daemonProcess = null;
let daemonReadyPromise = null;
let daemonStdoutBuffer = "";
let daemonRequestSerial = 0;
const daemonPending = new Map(); // id -> { resolve, reject, timer, command, args }
const respawnHistory = [];
let permanentlyFallback = BRIDGE_MODE_REQUESTED === "spawn";

function _now() { return Date.now(); }

function _spawnDaemon() {
  const proc = spawn(PYTHON, [BRIDGE, "--daemon"], {
    env: { ...process.env, PRISM_WORKSPACE: WORKSPACE },
  });

  proc.stdout.on("data", (chunk) => {
    daemonStdoutBuffer += chunk.toString("utf-8");
    let idx;
    while ((idx = daemonStdoutBuffer.indexOf("\n")) >= 0) {
      const line = daemonStdoutBuffer.slice(0, idx);
      daemonStdoutBuffer = daemonStdoutBuffer.slice(idx + 1);
      if (!line.trim()) continue;
      let parsed;
      try {
        parsed = JSON.parse(line);
      } catch {
        console.error(`  [bridge] non-JSON on stdout: ${line.slice(0, 200)}`);
        continue;
      }
      const id = parsed.id;
      const handler = id != null ? daemonPending.get(id) : null;
      if (!handler) {
        console.error(`  [bridge] orphan response (id=${id})`);
        continue;
      }
      clearTimeout(handler.timer);
      daemonPending.delete(id);
      if (parsed.ok) {
        handler.resolve(parsed.result);
      } else {
        handler.reject(new Error(parsed.error || "Unknown bridge error"));
      }
    }
  });

  proc.stderr.on("data", (d) => {
    // Forward daemon log output to our stderr so it shows up in MCP logs.
    process.stderr.write(d);
  });

  proc.on("error", (err) => {
    console.error(`  [bridge] spawn error: ${err.message}`);
  });

  proc.on("close", (code, signal) => {
    console.error(`  [bridge] daemon exited (code=${code}, signal=${signal})`);
    const stale = Array.from(daemonPending.values());
    daemonPending.clear();
    daemonStdoutBuffer = "";

    if (daemonProcess === proc) {
      daemonProcess = null;
      daemonReadyPromise = null;
    }

    // Track respawn rate. If we exceed the budget, give up on daemon mode
    // entirely for this server's lifetime.
    const now = _now();
    while (respawnHistory.length && now - respawnHistory[0] > RESPAWN_WINDOW_MS) {
      respawnHistory.shift();
    }
    respawnHistory.push(now);
    if (respawnHistory.length >= MAX_RESPAWNS_PER_WINDOW) {
      console.error(
        `  [bridge] ${MAX_RESPAWNS_PER_WINDOW} respawns within ${RESPAWN_WINDOW_MS}ms — falling back to spawn-per-call mode for the rest of this session`,
      );
      permanentlyFallback = true;
    }

    // Reject in-flight requests with a retryable flag so callBrain can
    // try once more (either by respawning the daemon, or via spawn-mode
    // if we've fallen back).
    for (const h of stale) {
      clearTimeout(h.timer);
      const err = new Error("Bridge process died mid-request");
      err._retryable = true;
      h.reject(err);
    }
  });

  return proc;
}

async function _ensureDaemon() {
  if (permanentlyFallback) {
    throw new Error("Daemon mode disabled (permanently fallen back to spawn)");
  }
  if (daemonProcess && daemonReadyPromise) {
    return daemonReadyPromise;
  }
  daemonProcess = _spawnDaemon();
  daemonReadyPromise = _handshake(daemonProcess).catch((err) => {
    // Handshake failed — kill the process and surface the error so the
    // caller falls back to spawn-mode.
    try { daemonProcess && daemonProcess.kill("SIGTERM"); } catch {}
    daemonProcess = null;
    daemonReadyPromise = null;
    throw err;
  });
  return daemonReadyPromise;
}

async function _handshake(proc) {
  // Send a ping. If we get a pong within HANDSHAKE_TIMEOUT_MS, the daemon
  // is alive (and bootstrap.sh has run if it was needed). Otherwise treat
  // as dead and force a fallback.
  const id = `handshake-${_now()}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      daemonPending.delete(id);
      reject(new Error("Daemon handshake timed out"));
    }, HANDSHAKE_TIMEOUT_MS);
    daemonPending.set(id, {
      resolve: () => { clearTimeout(timer); resolve(); },
      reject: (err) => { clearTimeout(timer); reject(err); },
      timer,
      command: "ping",
      args: {},
    });
    proc.stdin.write(JSON.stringify({ id, command: "ping", args: {}, workspace: WORKSPACE }) + "\n");
  });
}

function _callBrainDaemonOnce(command, args) {
  const id = `r${++daemonRequestSerial}`;
  const request = JSON.stringify({ id, command, args, workspace: WORKSPACE });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      daemonPending.delete(id);
      const err = new Error(`Bridge request timed out after ${REQUEST_TIMEOUT_MS}ms (command=${command})`);
      err._retryable = false;
      reject(err);
    }, REQUEST_TIMEOUT_MS);

    daemonPending.set(id, {
      resolve: (v) => { clearTimeout(timer); resolve(v); },
      reject: (e) => { clearTimeout(timer); reject(e); },
      timer,
      command,
      args,
    });

    const ok = daemonProcess.stdin.write(request + "\n");
    if (!ok) {
      // Backpressure or broken pipe — let the close handler reject this.
      // (We don't reject here because the close event will fire shortly.)
    }
  });
}

function _callBrainSpawn(command, args) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PYTHON, [BRIDGE], {
      env: { ...process.env, PRISM_WORKSPACE: WORKSPACE },
    });
    const request = JSON.stringify({ command, args, workspace: WORKSPACE });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("error", (err) => reject(new Error(`Failed to spawn ${PYTHON}: ${err.message}`)));
    proc.on("close", (code) => {
      if (code !== 0 && !stdout.trim()) {
        return reject(new Error(`Bridge exited ${code}: ${stderr}`));
      }
      try {
        const parsed = JSON.parse(stdout);
        if (!parsed.ok) {
          return reject(new Error(parsed.error || "Unknown bridge error"));
        }
        resolve(parsed.result);
      } catch (e) {
        reject(new Error(`Invalid JSON from bridge: ${stdout.slice(0, 500)}`));
      }
    });
    proc.stdin.write(request);
    proc.stdin.end();
  });
}

async function callBrain(command, args = {}) {
  if (permanentlyFallback) {
    return _callBrainSpawn(command, args);
  }
  try {
    await _ensureDaemon();
    return await _callBrainDaemonOnce(command, args);
  } catch (err) {
    if (err && err._retryable && !permanentlyFallback) {
      // The daemon died mid-request. Respawn handler already triggered;
      // try once more on the fresh daemon.
      try {
        await _ensureDaemon();
        return await _callBrainDaemonOnce(command, args);
      } catch (err2) {
        if (permanentlyFallback) {
          return _callBrainSpawn(command, args);
        }
        throw err2;
      }
    }
    if (permanentlyFallback) {
      return _callBrainSpawn(command, args);
    }
    throw err;
  }
}

// Make sure the daemon is shut down with the server.
function _cleanupDaemon() {
  if (daemonProcess) {
    try { daemonProcess.kill("SIGTERM"); } catch {}
    daemonProcess = null;
  }
}
process.on("exit", _cleanupDaemon);
process.on("SIGINT", () => { _cleanupDaemon(); process.exit(130); });
process.on("SIGTERM", () => { _cleanupDaemon(); process.exit(143); });

// ---------------------------------------------------------------------------
// Extension discovery & naming enforcement
// ---------------------------------------------------------------------------

// Workspace extension layout: <workspace>/prism/prism-extensions/<ext_id>/
const EXT_DIR_PATH = "prism/prism-extensions";
const EXT_ID_RE = /^[a-z][a-z0-9]*$/;

// Reserved core tool names — extensions cannot shadow these.
const CORE_TOOL_NAMES = new Set([
  "prism_core_search",
  "prism_core_ingest",
  "prism_core_inbox",
  "prism_core_ingest_url",
  "prism_core_ingest_text",
  "prism_core_reingest",
  "prism_core_source_delete",
  "prism_core_tag",
  "prism_core_graph",
  "prism_core_stats",
  "prism_core_export",
  "prism_core_index",
  "prism_core_domains_get",
  "prism_core_domains_set",
  "prism_core_axioms_get",
  "prism_core_axioms_revise",
  "prism_core_axioms_history",
  "prism_core_extract_concepts",
  "prism_core_propose_concept",
  "prism_core_review_proposals",
  "prism_core_accept_proposal",
  "prism_core_reject_proposal",
]);

function discoverExtensions() {
  const extDir = join(WORKSPACE, EXT_DIR_PATH);
  if (!existsSync(extDir)) return [];

  const extensions = [];
  for (const name of readdirSync(extDir, { withFileTypes: true })) {
    if (!name.isDirectory()) continue;
    const manifestPath = join(extDir, name.name, "manifest.json");
    if (!existsSync(manifestPath)) continue;
    try {
      const manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
      const extId = manifest.name || name.name;
      if (!EXT_ID_RE.test(extId)) {
        console.error(
          `  Extension ${name.name}: invalid id "${extId}" — must match ${EXT_ID_RE} ` +
          "(lowercase letters/digits, no underscores). Skipping.",
        );
        continue;
      }
      extensions.push({ dir: name.name, id: extId, path: join(extDir, name.name), manifest });
      console.error(`  Extension discovered: ${extId} v${manifest.version || "?"}`);
    } catch (e) {
      console.error(`  Extension ${name.name}: failed to load manifest (${e.message})`);
    }
  }
  return extensions;
}

// Restricted API surface for extensions. Core graph operations (search, addNode,
// addEdge, getGraphData, exportGraph, stats) are always available. Prediction
// CRUD is exposed because the predictions table lives in core but its tooling
// (HTTP, polling, narrative drafting) lives in the 51folds extension — the
// extension reads/writes the table via these sanctioned methods rather than
// reaching into brain.py directly.
function buildBrainAPI() {
  return {
    search: (query, top_k = 3) =>
      callBrain("search", { query, top_k }),
    addNode: (node_id, label, node_type, group_id = null, metadata = null) =>
      callBrain("add_node", { node_id, label, node_type, group_id, metadata }),
    addEdge: (source_id, target_id, edge_type, label = null) =>
      callBrain("add_edge", { source_id, target_id, edge_type, label }),
    getGraphData: () =>
      callBrain("get_graph_data"),
    exportGraph: () =>
      callBrain("export"),
    stats: () =>
      callBrain("stats"),
    // Prediction table access (B8 — table in core, tooling in extension)
    predictionSave: (model_id, question, fields = {}) =>
      callBrain("prediction_save", { model_id, question, ...fields }),
    predictionUpdate: (model_id, fields = {}) =>
      callBrain("prediction_update", { model_id, ...fields }),
    predictionGet: (model_id) =>
      callBrain("prediction_get", { model_id }),
    predictionList: (filters = {}) =>
      callBrain("prediction_list", filters),
    predictionIngestNarrative: (model_id, narrative_path = null) =>
      callBrain("prediction_ingest_narrative", { model_id, narrative_path }),
    // Source ingestion for predictions wanting to register results
    ingestText: (text, title = null, metadata = null, domain_hint = null) =>
      callBrain("ingest_text", { text, title, metadata, domain_hint }),
  };
}

function _validateExtensionRequires(manifest, brainAPI) {
  const requires = manifest.requires;
  if (!Array.isArray(requires) || requires.length === 0) return [];
  const available = new Set(Object.keys(brainAPI));
  return requires.filter((m) => !available.has(m));
}

// Tool-name policy: extension-declared tool names are bare identifiers
// (e.g. "refine_thesis"). The loader rewrites every name to
// `prism_<ext_id>_<bare_name>` before registering with the MCP server.
// Authors do not choose the final name. Collisions with core tools or with
// previously-loaded extensions are refused at load time.
const BARE_TOOL_NAME_RE = /^[a-z][a-z0-9_]*$/;

function _normaliseExtensionToolName(extId, bareName) {
  return `prism_${extId}_${bareName}`;
}

async function loadExtensionTools(extensions) {
  const brainAPI = buildBrainAPI();
  let totalTools = 0;
  const registeredExtensionToolNames = new Set();

  for (const ext of extensions) {
    const toolsPath = join(ext.path, "tools.js");
    if (!existsSync(toolsPath)) {
      console.error(`  Extension ${ext.id}: no tools.js found, skipping`);
      continue;
    }

    const missing = _validateExtensionRequires(ext.manifest, brainAPI);
    if (missing.length > 0) {
      console.error(
        `  Extension ${ext.id}: declares require for unknown brain API method(s): ${missing.join(", ")}. ` +
        `Available: ${Object.keys(brainAPI).join(", ")}. ` +
        `Loading anyway — calls to these methods will fail at runtime.`,
      );
    }

    try {
      const mod = await import(pathToFileURL(toolsPath).href);
      if (typeof mod.default !== "function") {
        console.error(`  Extension ${ext.id}: tools.js must export default function`);
        continue;
      }

      const tools = mod.default(brainAPI, z);
      if (!Array.isArray(tools)) {
        console.error(`  Extension ${ext.id}: register function must return an array`);
        continue;
      }

      let registered = 0;
      for (const tool of tools) {
        const bareName = tool.name;
        if (typeof bareName !== "string" || !BARE_TOOL_NAME_RE.test(bareName)) {
          console.error(
            `  Extension ${ext.id}: tool name "${bareName}" is invalid — must match ${BARE_TOOL_NAME_RE}. Skipping.`,
          );
          continue;
        }
        const finalName = _normaliseExtensionToolName(ext.id, bareName);
        if (CORE_TOOL_NAMES.has(finalName)) {
          console.error(
            `  Extension ${ext.id}: tool "${bareName}" would collide with core tool "${finalName}". Refusing to register.`,
          );
          continue;
        }
        if (registeredExtensionToolNames.has(finalName)) {
          console.error(
            `  Extension ${ext.id}: tool "${finalName}" already registered by another extension. Refusing to register.`,
          );
          continue;
        }
        server.registerTool(
          finalName,
          { description: tool.description, inputSchema: tool.inputSchema },
          async (args) => {
            const result = await tool.handler(args, brainAPI);
            return {
              content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
            };
          }
        );
        registeredExtensionToolNames.add(finalName);
        registered++;
        totalTools++;
      }
      console.error(`  Extension ${ext.id}: ${registered} tool(s) registered (prism_${ext.id}_*)`);
    } catch (e) {
      console.error(`  Extension ${ext.id}: failed to load tools (${e.message})`);
    }
  }
  return totalTools;
}

// ---------------------------------------------------------------------------
// MCP server
// ---------------------------------------------------------------------------

const server = new McpServer(
  { name: "prism", version: "2.0.0" },
);

// --- prism_core_search ---
server.registerTool(
  "prism_core_search",
  {
    description:
      "Search the PRISM knowledge base using hybrid search (BM25 keyword + semantic vector, fused via RRF). Returns the most relevant chunks from ingested sources. Set include_reference=true to also search the reference brain.",
    inputSchema: z.object({
      query: z.string().describe("The search query"),
      top_k: z
        .number()
        .int()
        .min(1)
        .max(20)
        .default(3)
        .describe("Number of results to return"),
      include_reference: z
        .boolean()
        .default(false)
        .describe("Also search the reference brain and include results"),
    }),
  },
  async ({ query, top_k, include_reference }) => {
    const results = await callBrain("search", { query, top_k });
    const response = { results };

    if (include_reference) {
      try {
        const refResults = await callBrain("search_reference", { query, top_k });
        response.reference_results = refResults;
      } catch {
        response.reference_results = [];
      }
    }

    return {
      content: [{ type: "text", text: JSON.stringify(response, null, 2) }],
    };
  }
);

// --- prism_core_ingest ---
server.registerTool(
  "prism_core_ingest",
  {
    description:
      "Process all files in the prism-inbox/ folder: chunk, embed, classify domains, wire graph, export. Files move to prism-sources/ after processing.",
    inputSchema: z.object({}),
  },
  async () => {
    const results = await callBrain("ingest");
    return {
      content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
    };
  }
);

// --- prism_core_inbox (alias) ---
server.registerTool(
  "prism_core_inbox",
  {
    description: "Alias for prism_core_ingest — processes the prism-inbox/ folder.",
    inputSchema: z.object({}),
  },
  async () => {
    const results = await callBrain("ingest");
    return {
      content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
    };
  }
);

// --- prism_core_tag ---
server.registerTool(
  "prism_core_tag",
  {
    description:
      "Manually assign or correct domain classifications for a source. Rewires graph edges and regenerates the explorer.",
    inputSchema: z.object({
      source_id: z
        .string()
        .describe('Source ID (e.g. "S01", "S14")'),
      domains: z
        .array(z.number().int())
        .describe("Array of domain numbers to assign"),
    }),
  },
  async ({ source_id, domains }) => {
    const result = await callBrain("tag", { source_id, domains });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_graph ---
server.registerTool(
  "prism_core_graph",
  {
    description:
      "Query the knowledge graph: return all nodes and edges within N hops of a given node.",
    inputSchema: z.object({
      node_id: z.string().describe('Node ID (e.g. "C01", "D1", "S05")'),
      hops: z
        .number()
        .int()
        .min(1)
        .max(5)
        .default(1)
        .describe("Number of hops to traverse"),
    }),
  },
  async ({ node_id, hops }) => {
    const result = await callBrain("graph", { node_id, hops });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_stats ---
server.registerTool(
  "prism_core_stats",
  {
    description:
      "Return current brain statistics: source count, chunk count, node count, edge count, domain count, cross-domain edges, embedding count.",
    inputSchema: z.object({}),
  },
  async () => {
    const result = await callBrain("stats");
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_export ---
server.registerTool(
  "prism_core_export",
  {
    description:
      "Regenerate the graph explorer (prism-graph-explorer.html) and graph data (prism-graph.json) from the database.",
    inputSchema: z.object({}),
  },
  async () => {
    const result = await callBrain("export");
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_index ---
server.registerTool(
  "prism_core_index",
  {
    description:
      "Return the full source registry: every source with its ID, title, assigned domains, and chunk count.",
    inputSchema: z.object({}),
  },
  async () => {
    const result = await callBrain("index");
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_extract_concepts ---
server.registerTool(
  "prism_core_extract_concepts",
  {
    description:
      "Get extraction context for a source: its chunks plus the existing graph structure. Use this after ingestion to identify concepts. Read the context, then call prism_core_propose_concept for each concept you identify.",
    inputSchema: z.object({
      source_id: z
        .string()
        .describe('Source ID to extract concepts from (e.g. "S01")'),
    }),
  },
  async ({ source_id }) => {
    const result = await callBrain("extract_context", { source_id });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_propose_concept ---
server.registerTool(
  "prism_core_propose_concept",
  {
    description:
      "Propose a new concept node extracted from a source. The concept starts as 'proposed' and must be accepted by the user via prism_core_accept_proposal.",
    inputSchema: z.object({
      concept_id: z
        .string()
        .describe('Concept ID (use the next_concept_id from extract_concepts)'),
      label: z
        .string()
        .describe("Precise concept name using source terminology"),
      domain_id: z
        .number()
        .int()
        .describe("Domain group number this concept belongs to"),
      source_id: z
        .string()
        .describe("Source ID this concept was extracted from"),
      edges: z
        .array(
          z.object({
            target: z.string().describe("Target node ID"),
            type: z.string().describe("Edge type (relates_to, enables, causes, etc.)"),
            label: z.string().optional().describe("Edge description"),
          })
        )
        .optional()
        .default([])
        .describe("Proposed edges to existing nodes"),
    }),
  },
  async ({ concept_id, label, domain_id, source_id, edges }) => {
    const result = await callBrain("propose_concept", {
      concept_id, label, domain_id, source_id, edges,
    });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_review_proposals ---
server.registerTool(
  "prism_core_review_proposals",
  {
    description:
      "List all proposed concepts awaiting user review. Present each as a review card for the user to accept, reject, or modify.",
    inputSchema: z.object({}),
  },
  async () => {
    const result = await callBrain("list_proposals");
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_accept_proposal ---
server.registerTool(
  "prism_core_accept_proposal",
  {
    description:
      "Accept a proposed concept, making it a permanent node in the knowledge graph.",
    inputSchema: z.object({
      concept_id: z
        .string()
        .describe("Concept ID to accept (e.g. C15)"),
    }),
  },
  async ({ concept_id }) => {
    const result = await callBrain("accept_proposal", { concept_id });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_reject_proposal ---
server.registerTool(
  "prism_core_reject_proposal",
  {
    description:
      "Reject a proposed concept. Removes the node and its edges, and logs the rejection for future extraction context.",
    inputSchema: z.object({
      concept_id: z
        .string()
        .describe("Concept ID to reject"),
      reason: z
        .string()
        .optional()
        .default("")
        .describe("Why this concept was rejected (helps improve future extractions)"),
    }),
  },
  async ({ concept_id, reason }) => {
    const result = await callBrain("reject_proposal", { concept_id, reason });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_domains_get ---
server.registerTool(
  "prism_core_domains_get",
  {
    description:
      "Return the user's domains as recorded in prism-brain.db. Each domain has id, label, short_label, color, keywords (the classifier description), and timestamps.",
    inputSchema: z.object({}),
  },
  async () => {
    const result = await callBrain("domains_get");
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_domains_set ---
server.registerTool(
  "prism_core_domains_set",
  {
    description:
      "Create or update a domain in prism-brain.db. If domain_id is provided, updates that row; otherwise inserts (or merges with an existing label match). Updating keywords improves classifier accuracy for future ingestions.",
    inputSchema: z.object({
      label: z.string().describe("Display name of the domain (e.g. 'Energy & Infrastructure')"),
      keywords: z
        .string()
        .default("")
        .describe("Keyword description used by the classifier (paragraph of relevant terms)"),
      short_label: z
        .string()
        .optional()
        .describe("Short label for graph legend (defaults to label)"),
      color: z
        .string()
        .optional()
        .describe("Hex colour for graph rendering (auto-assigned if omitted)"),
      domain_id: z
        .number()
        .int()
        .optional()
        .describe("Existing domain id to update; omit to insert/merge by label"),
    }),
  },
  async ({ label, keywords, short_label, color, domain_id }) => {
    const result = await callBrain("domains_set", {
      label, keywords, short_label, color, domain_id,
    });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_axioms_get ---
server.registerTool(
  "prism_core_axioms_get",
  {
    description:
      "Return the user's active axioms from prism-brain.db. Each axiom has key, body, citations (list of source IDs), boundary (where it breaks), and timestamps. The axioms table is the source of truth; prism-axioms.md is a regenerated read-only projection.",
    inputSchema: z.object({
      active_only: z
        .boolean()
        .default(true)
        .describe("If true (default), excludes superseded axioms"),
    }),
  },
  async ({ active_only }) => {
    const result = await callBrain("axioms_get", { active_only });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_axioms_revise ---
server.registerTool(
  "prism_core_axioms_revise",
  {
    description:
      "Add or revise an axiom. If a previous axiom exists for the given key, it is marked superseded and the new one becomes active. Axioms are authored through conversation — you draft from dialogue with the user, the user accepts/sharpens/rejects, then you call this tool. prism-axioms.md is regenerated automatically.",
    inputSchema: z.object({
      key: z
        .string()
        .describe("Short identifier for the axiom (e.g. 'cumulative-recombination', 'ai-energy-floor')"),
      body: z
        .string()
        .describe("The axiom text — what the user is claiming"),
      citations: z
        .array(z.string())
        .optional()
        .describe("Source IDs that support the claim (e.g. ['S07','S14'])"),
      boundary: z
        .string()
        .optional()
        .describe("Where the axiom breaks down — every axiom has one"),
    }),
  },
  async ({ key, body, citations, boundary }) => {
    const result = await callBrain("axioms_revise", { key, body, citations, boundary });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_axioms_history ---
server.registerTool(
  "prism_core_axioms_history",
  {
    description:
      "Return all revisions for an axiom key in chronological order. Use this to see how an axiom has evolved.",
    inputSchema: z.object({
      key: z.string().describe("Axiom key"),
    }),
  },
  async ({ key }) => {
    const result = await callBrain("axioms_history", { key });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_ingest_url ---
server.registerTool(
  "prism_core_ingest_url",
  {
    description:
      "Fetch a URL, extract the article text, and ingest as a new source. Use this when the user says 'ingest this URL' or 'add this article from <link>'.",
    inputSchema: z.object({
      url: z.string().describe("The URL to fetch"),
      title: z
        .string()
        .optional()
        .describe("Optional title (defaults to the page <title>)"),
      domain_hint: z
        .string()
        .optional()
        .describe("Optional domain hint from the user's prompt context"),
    }),
  },
  async ({ url, title, domain_hint }) => {
    const result = await callBrain("ingest_url", { url, title, domain_hint });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_ingest_text ---
server.registerTool(
  "prism_core_ingest_text",
  {
    description:
      "Ingest pasted text as a new source. Use this when the user pastes a transcript, excerpt, or passage into the prompt and asks to add it to the brain.",
    inputSchema: z.object({
      text: z.string().describe("The full text content"),
      title: z.string().optional().describe("Optional title for the source"),
      metadata: z
        .record(z.string())
        .optional()
        .describe("Optional metadata (author, source URL, date)"),
      domain_hint: z
        .string()
        .optional()
        .describe("Optional domain hint from the user's prompt context"),
    }),
  },
  async ({ text, title, metadata, domain_hint }) => {
    const result = await callBrain("ingest_text", { text, title, metadata, domain_hint });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_reingest ---
server.registerTool(
  "prism_core_reingest",
  {
    description:
      "Re-read a source file in prism-sources/, regenerate chunks and embeddings. Use this if the file has been corrected or replaced and you want the brain to pick up the new content. Graph wiring is preserved.",
    inputSchema: z.object({
      source_id: z.string().describe("Source ID to re-ingest (e.g. 'S07')"),
    }),
  },
  async ({ source_id }) => {
    const result = await callBrain("reingest", { source_id });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// --- prism_core_source_delete ---
server.registerTool(
  "prism_core_source_delete",
  {
    description:
      "Remove a source from the brain — deletes the node, chunks, embeddings, and edges. Does not delete the file in prism-sources/ (the user can do that manually if they want).",
    inputSchema: z.object({
      source_id: z.string().describe("Source ID to delete (e.g. 'S07')"),
    }),
  },
  async ({ source_id }) => {
    const result = await callBrain("source_delete", { source_id });
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  }
);

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

async function main() {
  console.error("PRISM MCP Server starting...");
  console.error(`  Workspace: ${WORKSPACE}`);
  console.error(`  Engine: ${ENGINE_DIR}`);

  // First-run bootstrap: create workspace structure if needed
  try {
    const bootstrap = await callBrain("bootstrap");
    if (bootstrap.is_new) {
      console.error("  First run detected — workspace bootstrapped:");
      for (const item of bootstrap.created) {
        console.error(`    + ${item}`);
      }
    } else {
      console.error(`  Workspace ready (${bootstrap.stats.sources} sources, ${bootstrap.stats.chunks} chunks)`);
    }
  } catch (e) {
    console.error(`  Bootstrap warning: ${e.message}`);
  }

  // Discover and load extensions
  const extensions = discoverExtensions();
  if (extensions.length > 0) {
    const extToolCount = await loadExtensionTools(extensions);
    console.error(`  Extensions: ${extensions.length} loaded, ${extToolCount} tool(s) registered`);
  }

  // Connect via stdio
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("PRISM MCP Server running on stdio");
}

main().catch((e) => {
  console.error(`Fatal: ${e.message}`);
  process.exit(1);
});

export {
  callBrain,
  discoverExtensions,
  loadExtensionTools,
  buildBrainAPI,
  _validateExtensionRequires,
  _normaliseExtensionToolName,
  CORE_TOOL_NAMES,
  EXT_DIR_PATH,
  EXT_ID_RE,
  BARE_TOOL_NAME_RE,
  server,
};
