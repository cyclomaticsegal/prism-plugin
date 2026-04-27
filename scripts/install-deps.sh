#!/usr/bin/env bash
# install-deps.sh — Idempotent dependency setup for PRISM.
#
# Runs from the SessionStart hook. The Cowork VM is recreated between
# sessions, but ${CLAUDE_PLUGIN_DATA} (mapped to ~/.claude/plugins/data/<id>/
# on the host) is the canonical persistent store for plugin state. Node
# modules and the Python venv live there so they survive plugin updates and,
# where Cowork preserves it, survive VM resets.
#
# Set PRISM_AUTO_BOOTSTRAP=0 to disable. Manual installation steps then
# replace this hook — see README "Manual dependencies".

set -e

if [ "${PRISM_AUTO_BOOTSTRAP:-1}" = "0" ]; then
  echo "[prism] PRISM_AUTO_BOOTSTRAP=0 — skipping auto-install." >&2
  exit 0
fi

ROOT="${CLAUDE_PLUGIN_ROOT:?CLAUDE_PLUGIN_ROOT must be set by Claude Code}"
DATA="${CLAUDE_PLUGIN_DATA:?CLAUDE_PLUGIN_DATA must be set by Claude Code}"

mkdir -p "$DATA"

# ---- Node deps ---------------------------------------------------------------
SRC_PKG="$ROOT/server/package.json"
DST_PKG="$DATA/package.json"
if ! diff -q "$SRC_PKG" "$DST_PKG" >/dev/null 2>&1; then
  echo "[prism] installing Node dependencies into \$CLAUDE_PLUGIN_DATA..." >&2
  cp "$SRC_PKG" "$DST_PKG"
  cp "$ROOT/server/package-lock.json" "$DATA/package-lock.json" 2>/dev/null || true
  if (cd "$DATA" && npm install --silent --no-audit --no-fund >&2); then
    echo "[prism] Node deps ready" >&2
  else
    rm -f "$DST_PKG"
    echo "[prism] npm install failed; will retry next session" >&2
    exit 1
  fi
fi

# ---- ESM resolution shim -----------------------------------------------------
# Node's ESM loader does not honour NODE_PATH for bare specifiers — the only
# resolution path it walks is "node_modules/" siblings up from the importing
# file. server/index.js uses ESM imports, so we must surface the data-dir
# node_modules next to it. A symlink is enough: it survives plugin updates
# (the next session re-creates it) and weighs nothing on disk.
SERVER_LINK="$ROOT/server/node_modules"
if [ -L "$SERVER_LINK" ]; then
  rm -f "$SERVER_LINK"
elif [ -e "$SERVER_LINK" ]; then
  rm -rf "$SERVER_LINK"
fi
ln -s "$DATA/node_modules" "$SERVER_LINK"

# ---- Python deps (in a venv inside DATA) ------------------------------------
VENV="$DATA/venv"
SRC_REQ="$ROOT/engine/requirements.txt"
DST_REQ="$DATA/requirements.txt"
NEED_INSTALL=0
[ ! -x "$VENV/bin/python3" ] && NEED_INSTALL=1
diff -q "$SRC_REQ" "$DST_REQ" >/dev/null 2>&1 || NEED_INSTALL=1

if [ "$NEED_INSTALL" = "1" ]; then
  echo "[prism] installing Python dependencies into \$CLAUDE_PLUGIN_DATA/venv..." >&2
  if [ ! -x "$VENV/bin/python3" ]; then
    python3 -m venv "$VENV"
  fi
  if "$VENV/bin/pip" install --quiet -r "$SRC_REQ"; then
    cp "$SRC_REQ" "$DST_REQ"
    echo "[prism] Python deps ready" >&2
  else
    rm -f "$DST_REQ"
    echo "[prism] pip install failed; will retry next session" >&2
    exit 1
  fi
fi

echo "[prism] dependencies ready" >&2
