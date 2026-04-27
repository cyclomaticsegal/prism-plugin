/**
 * 51Folds API client — HTTP wrappers for api.51folds.ai and app.51folds.ai.
 *
 * Two endpoints:
 *   - api.51folds.ai/api/v1/...        — documented API for CRUD
 *   - app.51folds.ai/api/platform/v1/... — platform API with progress + rich results
 *
 * Auth: Bearer token from PRISM_FOLDS_API_TOKEN env var or .env in this folder.
 */

import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const __dirname = dirname(fileURLToPath(import.meta.url));

const API_BASE = process.env.PRISM_FOLDS_API_URL || "https://api.51folds.ai";
const PLATFORM_BASE = process.env.PRISM_FOLDS_PLATFORM_URL || "https://app.51folds.ai";

let _token = null;

function _loadToken() {
  if (_token) return _token;

  if (process.env.PRISM_FOLDS_API_TOKEN) {
    _token = process.env.PRISM_FOLDS_API_TOKEN;
    return _token;
  }

  const envFile = join(__dirname, ".env");
  if (existsSync(envFile)) {
    const lines = readFileSync(envFile, "utf-8").split(/\r?\n/);
    for (const line of lines) {
      const m = line.match(/^\s*PRISM_FOLDS_API_TOKEN\s*=\s*(.+?)\s*$/);
      if (m) {
        _token = m[1].replace(/^['"]|['"]$/g, "");
        return _token;
      }
    }
  }

  throw new Error(
    "51Folds: PRISM_FOLDS_API_TOKEN not found. Set the env var or create prism/prism-extensions/folds/.env from .env.example."
  );
}

async function _request(base, method, path, { body, extraHeaders, timeoutMs } = {}) {
  const token = _loadToken();
  const url = `${base}${path}`;
  const headers = {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
    ...(extraHeaders || {}),
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || 60000);

  try {
    const resp = await fetch(url, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const text = await resp.text();
    let parsed;
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      return { error: `Non-JSON response (${resp.status}): ${text.slice(0, 300)}`, status_code: resp.status };
    }
    if (!resp.ok) {
      return { error: parsed.message || parsed.error || `HTTP ${resp.status}`, status_code: resp.status, body: parsed };
    }
    return parsed;
  } catch (err) {
    if (err.name === "AbortError") return { error: "Request timed out", status_code: -1 };
    return { error: String(err), status_code: -1 };
  } finally {
    clearTimeout(timer);
  }
}

// --- Documented API (api.51folds.ai) ---

export function checkCredits() {
  return _request(API_BASE, "GET", "/api/v1/credits/me");
}

export function createModel({
  question,
  outcomes,
  additionalContext,
  modelType = "Advanced",
  generateDrivers = true,
  generateTakeaway = true,
}) {
  const validTypes = new Set(["Overview", "Insight", "Advanced"]);
  let type = modelType;
  if (!validTypes.has(type)) {
    type = type[0]?.toUpperCase() + type.slice(1).toLowerCase();
    if (!validTypes.has(type)) {
      return Promise.resolve({ error: `Invalid model type: ${modelType}. Must be Overview, Insight, or Advanced.` });
    }
  }

  return _request(API_BASE, "POST", "/api/v1/models", {
    body: {
      question,
      outcomes,
      additionalContext,
      type,
      count: 1,
      generateDriverContent: generateDrivers,
      generateTakeAwayContent: generateTakeaway,
    },
    extraHeaders: { "X-Idempotency-Key": randomUUID() },
    timeoutMs: 60000,
  });
}

export function getModel(modelId) {
  return _request(API_BASE, "GET", `/api/v1/models/${modelId}`);
}

export function getReport(modelId) {
  return _request(API_BASE, "GET", `/api/v1/models/${modelId}/reports`);
}

export function createReport(modelId) {
  return _request(API_BASE, "POST", `/api/v1/models/${modelId}/reports`);
}

// --- Platform API (app.51folds.ai) ---

export function getPlatformModel(modelId) {
  return _request(PLATFORM_BASE, "GET", `/api/platform/v1/models/${modelId}`);
}

export async function getModelProgress(modelId) {
  const resp = await getPlatformModel(modelId);
  if (resp.error) return resp;
  const data = resp.data || {};
  return {
    model_id: modelId,
    progress: data.progress ?? 0,
    status: data.status ?? -1,
    statusLabel: data.statusLabel || "unknown",
    shortSummary: data.shortSummary || "",
    updatedAt: data.updatedAt || "",
  };
}

export async function getModelResults(modelId) {
  const resp = await getPlatformModel(modelId);
  if (resp.error) return resp;
  const data = resp.data || {};
  return {
    model_id: modelId,
    progress: data.progress ?? 0,
    status: data.status ?? -1,
    statusLabel: data.statusLabel || "",
    shortSummary: data.shortSummary || "",
    result: data.result || null,
    nodes: data.nodes || [],
    states: data.states || [],
    edges: data.edges || [],
    justification: data.justification || "",
    descriptions: data.descriptions || {},
  };
}
