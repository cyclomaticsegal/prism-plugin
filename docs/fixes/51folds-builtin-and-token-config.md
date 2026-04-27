# Fix: ship 51Folds as a built-in part of the plugin

## The bug

The user's expectation: install the PRISM plugin, paste a 51Folds API token, get the four `prism_folds_*` tools wired into every brain. That is the "whole idea" — 51Folds is part of the plugin, not a side install.

The implementation does not deliver that:

1. **51Folds is not auto-loaded.** `server/index.js`'s `discoverExtensions()` scans only `<workspace>/prism/prism-extensions/`. The bundled `extensions/51folds/` directory at the plugin root is never read, so no `prism_folds_*` tool registers without the user manually copying the folder into their workspace.
2. **The API token has no Cowork-native prompt.** `client.js` resolves `PRISM_FOLDS_API_TOKEN` from `process.env` then a `.env` file. Neither is wired into the plugin's `.mcp.json` or `plugin.json`, so Cowork never asks the user for it. The user has to set the env var on the host or hand-create a `.env` file inside their workspace.

The combined effect: out-of-the-box, a freshly installed PRISM plugin has zero 51Folds capability, even with a valid API token sitting on the host.

## The intended behaviour

End-user flow on a clean install:

1. `/plugin marketplace add cyclomaticsegal/prism-plugin`
2. `/plugin install prism@cyclomaticsegal`
3. Cowork prompts once for the 51Folds API token (sensitive userConfig field). User pastes it.
4. Open Cowork against any folder. Both the core `prism_core_*` tools and the four `prism_folds_*` tools are available immediately. Token comes from the OS keychain via `${user_config.folds_api_token}`.

No file copying. No `.env` editing. No host env vars.

## The fix — two changes

### 1. Auto-load extensions bundled with the plugin

Change `discoverExtensions()` in `server/index.js` to scan **two** roots, in this order:

- `${CLAUDE_PLUGIN_ROOT}/extensions/` — extensions shipped with the plugin (51Folds, plus any future built-ins). Loaded for every workspace.
- `<workspace>/prism/prism-extensions/` — user-installed extensions specific to one brain. Loaded only for the workspace where they live.

Naming-collision rules already enforced by the loader stay in place. Workspace-local extensions take precedence over bundled ones with the same id, so a user can override a built-in by dropping their own copy into the workspace.

Files to touch:
- `server/index.js` — extend `EXT_DIR_PATH` constant + `discoverExtensions()` to walk both roots and concatenate results.
- `tests/test_extensions.py` — add a case that registers a bundled extension from the plugin root and confirms its tools appear without anything in the workspace.

### 2. API token via Cowork's `userConfig` (sensitive)

Add to `.claude-plugin/plugin.json`:

```json
"userConfig": {
  "folds_api_token": {
    "type": "string",
    "title": "51Folds API token",
    "description": "Bearer token from app.51folds.ai/account/tokens. Leave blank if you don't use 51Folds — the prism_folds_* tools register but fail at first call.",
    "sensitive": true
  }
}
```

Add to `.mcp.json` env:

```json
"PRISM_FOLDS_API_TOKEN": "${user_config.folds_api_token}"
```

`extensions/51folds/client.js` already reads `process.env.PRISM_FOLDS_API_TOKEN` first, so no extension-side change is required. The `.env` fallback can stay as a per-workspace override for users who want a different token in different brains.

`extensions/51folds/.env.example` should be updated to mention the userConfig flow as primary and the `.env` file as the per-workspace override.

## Risks / tradeoffs to think about before shipping

- **Plugin-level token vs per-brain token.** `userConfig` values are plugin-wide. A user with multiple 51Folds accounts (one personal, one client) can't have different tokens per brain via this path — they'd fall back to the per-workspace `.env` override (still supported). Document this clearly.
- **Required vs optional userConfig field.** Marking it required forces every PRISM user to think about 51Folds even if they never plan to use it. Mark it optional. If empty, the four `prism_folds_*` tools should still register but return a clean "no token configured" error on first call rather than crashing the server. Verify this is what `client.js` does today.
- **Auto-loading scope creep.** Once the plugin auto-loads its own extensions, it's tempting to ship more of them. Be explicit about what does and doesn't get auto-loaded. Suggest a `bundled: true` field in extension manifests, or a hard list, or the convention "everything under `${CLAUDE_PLUGIN_ROOT}/extensions/` is bundled, full stop". Pick one and document.
- **Cache invalidation when a bundled extension is updated.** A plugin update changes `${CLAUDE_PLUGIN_ROOT}/extensions/<id>/`. Each Cowork session re-runs `discoverExtensions()` at MCP server startup, so updates are picked up automatically — confirm this in a test.

## Out of scope for this fix

- Adding more bundled extensions (only 51Folds today).
- Generalising the token-via-userConfig pattern to a per-extension declared schema. That can be a follow-up once we have a second extension that needs configuration.
- Telemetry or usage limits (51Folds API has its own rate limiting; no client-side accounting needed yet).

## Verification checklist for the fix branch

- [ ] Fresh `/plugin install prism@cyclomaticsegal` against a clean Cowork — Cowork prompts for the 51Folds token.
- [ ] After install, opening Cowork against an empty folder → `prism_folds_*` tools appear in Claude's toolset alongside `prism_core_*`.
- [ ] `prism_folds_refine_thesis` runs end-to-end against the live API.
- [ ] Workspace-local override: dropping `prism/prism-extensions/folds/.env` with a different token in one workspace overrides the keychain value for that brain only.
- [ ] All 235 existing tests still pass; `tests/test_extensions.py` gains a bundled-extension case.
