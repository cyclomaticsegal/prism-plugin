#!/usr/bin/env python3
"""Bridge between MCP server (Node.js) and brain engine (Python).

Two modes:

* **Single-shot** (default — back-compat with subprocess-per-call callers and
  the existing test_server.py harness): read one JSON request on stdin, write
  one JSON response on stdout, exit.

* **Daemon** (`bridge.py --daemon`): keep a single Python process alive; read
  newline-delimited JSON requests on stdin, write newline-delimited JSON
  responses on stdout, until stdin closes. The engine is imported once;
  every subsequent call reuses the in-memory state. This eliminates the
  ~1–2s cold-start penalty (sklearn import dominates) on every tool call.

Request format (both modes):
    {"id": "<opt>", "command": "<cmd>", "args": {...}, "workspace": "/path"}

Response format (both modes):
    {"id": "<echoed>", "ok": true,  "result": ...}
    {"id": "<echoed>", "ok": false, "error": "message"}

The `id` field is opaque — the bridge echoes whatever the caller sends.
The single-shot path tolerates absence of `id` (legacy callers).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Eager-import brain at module load time so callers that `import bridge`
# (the test suite, in-process callers) immediately get a working module.
# The daemon-startup path additionally calls _load_brain() to recover from
# missing deps via bootstrap.sh — that recovery runs only if the eager
# import failed.
ENGINE_DIR = Path(__file__).parent
PLUGIN_ROOT = ENGINE_DIR.parent
sys.path.insert(0, str(ENGINE_DIR))

brain = None
_eager_import_error = None
try:
    import brain as _brain  # noqa: E402
    brain = _brain
except ImportError as _e:
    _eager_import_error = _e


def _load_brain():
    """Ensure brain is importable. Run bootstrap.sh on first ImportError, retry once.

    Idempotent — safe to call multiple times. If the eager import at module
    load time succeeded, this is a no-op. If it failed (missing Python deps),
    this runs `engine/bootstrap.sh` exactly once and reattempts the import.
    """
    global brain, _eager_import_error
    if brain is not None:
        return brain

    bootstrap = ENGINE_DIR / "bootstrap.sh"
    if not bootstrap.exists():
        raise _eager_import_error or ImportError("brain module unavailable and bootstrap.sh missing")

    print(
        f"  [daemon] engine import failed ({_eager_import_error}); running bootstrap.sh...",
        file=sys.stderr,
    )
    try:
        subprocess.run(
            ["bash", str(bootstrap)],
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=300,
        )
    except subprocess.SubprocessError as bs_err:
        raise ImportError(
            f"engine import failed and bootstrap.sh did not recover: {bs_err}"
        ) from _eager_import_error

    import importlib
    if "brain" in sys.modules:
        brain = importlib.reload(sys.modules["brain"])
    else:
        import brain as _brain_retry  # noqa: F401
        brain = _brain_retry
    _eager_import_error = None
    return brain


def _ensure_runtime_deps():
    """Verify the heavy Python deps the engine needs are actually installed.

    `import brain` succeeds whether or not sklearn/numpy/sentence-transformers
    are present, because brain.py imports them lazily inside embedder
    __init__ methods rather than at module level. Without this check, the
    daemon would spawn cleanly, answer pings, and then fail on the first
    real tool call with an opaque ImportError. This probes for the heavy
    deps at startup; if any are missing it runs bootstrap.sh and retries.
    Idempotent — pip-install of present packages is fast.
    """
    needed = ["sklearn", "numpy"]
    missing = []
    for name in needed:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)

    if not missing:
        return

    bootstrap = ENGINE_DIR / "bootstrap.sh"
    if not bootstrap.exists():
        raise ImportError(
            f"runtime deps missing ({missing}) and bootstrap.sh not found"
        )

    print(
        f"  [daemon] runtime deps missing ({', '.join(missing)}); running bootstrap.sh...",
        file=sys.stderr,
    )
    sys.stderr.flush()
    try:
        subprocess.run(
            ["bash", str(bootstrap)],
            check=True,
            stdout=sys.stderr,
            stderr=sys.stderr,
            timeout=300,
        )
    except subprocess.SubprocessError as bs_err:
        raise ImportError(
            f"runtime deps missing ({missing}) and bootstrap.sh failed: {bs_err}"
        )

    still_missing = []
    for name in needed:
        if name in sys.modules:
            # Force re-check in case a previous failed import is cached.
            del sys.modules[name]
        try:
            __import__(name)
        except ImportError:
            still_missing.append(name)

    if still_missing:
        raise ImportError(
            f"runtime deps still missing after bootstrap.sh: {still_missing}"
        )


def _get_reference_db_path() -> Path:
    """Locate the reference brain database."""
    ref = PLUGIN_ROOT / "reference-brain" / "brain-reference.db"
    if ref.exists():
        return ref
    return None


def handle(request: dict) -> dict:
    """Dispatch a single request. Returns the result payload (not the envelope).

    The result is whatever the matching command returns — the caller wraps it
    in {ok, result, id} or {ok, error, id}.
    """
    cmd = request["command"]
    args = request.get("args", {})

    if cmd == "ping":
        return {"pong": True, "pid": os.getpid()}

    if cmd == "search":
        results = brain.search(
            args["query"],
            top_k=args.get("top_k", brain.DEFAULT_TOP_K),
        )
        conn = brain.init_db()
        enriched = []
        for r in results:
            node = conn.execute(
                "SELECT label, group_id FROM nodes WHERE id=?",
                (r["source_id"],),
            ).fetchone()
            enriched.append({
                "chunk_id": r["chunk_id"],
                "source_id": r["source_id"],
                "source_title": node[0] if node else r["source_id"],
                "domain_group": node[1] if node else None,
                "score": r.get("rrf_score", r.get("score", 0)),
                "excerpt": r["content"][:500],
                "method": r["method"],
            })
        conn.close()
        return enriched

    elif cmd == "search_reference":
        ref_db = _get_reference_db_path()
        if not ref_db or not ref_db.exists():
            return []
        original_db = brain.DB_PATH
        try:
            brain.DB_PATH = ref_db
            results = brain.search(
                args["query"],
                top_k=args.get("top_k", brain.DEFAULT_TOP_K),
            )
            conn = brain.init_db()
            enriched = []
            for r in results:
                node = conn.execute(
                    "SELECT label, group_id FROM nodes WHERE id=?",
                    (r["source_id"],),
                ).fetchone()
                enriched.append({
                    "chunk_id": r["chunk_id"],
                    "source_id": r["source_id"],
                    "source_title": node[0] if node else r["source_id"],
                    "domain_group": node[1] if node else None,
                    "score": r.get("rrf_score", r.get("score", 0)),
                    "excerpt": r["content"][:500],
                    "method": r["method"],
                    "source": "reference",
                })
            conn.close()
            return enriched
        finally:
            brain.DB_PATH = original_db
            brain.reset_embedder()

    elif cmd == "ingest":
        processed = brain.process_inbox()
        results = []
        for p in processed:
            results.append({
                "source_id": p["source_id"],
                "title": p["target"],
                "domains": p.get("domains", []),
            })
        return results

    elif cmd == "tag":
        source_id = args["source_id"].upper()
        domain_nums = [int(d) for d in args["domains"]]
        brain.tag_source(source_id, domain_nums)
        return {
            "status": "ok",
            "source_id": source_id,
            "domains": domain_nums,
        }

    elif cmd == "graph":
        return brain.graph_neighbors(
            args["node_id"],
            hops=args.get("hops", 1),
        )

    elif cmd == "stats":
        return brain.stats_dict()

    elif cmd == "export":
        brain.export_graph_json()
        return {"status": "ok"}

    elif cmd == "index":
        return brain.source_registry()

    elif cmd == "add_node":
        inserted = brain.add_node(
            args["node_id"], args["label"], args["node_type"],
            group_id=args.get("group_id"),
            metadata=args.get("metadata"),
        )
        return {"node_id": args["node_id"], "inserted": inserted}

    elif cmd == "add_edge":
        inserted = brain.add_edge(
            args["source_id"], args["target_id"], args["edge_type"],
            label=args.get("label"),
        )
        return {"source_id": args["source_id"], "target_id": args["target_id"], "inserted": inserted}

    elif cmd == "get_graph_data":
        return brain.get_graph_data()

    elif cmd == "extract_context":
        return brain.get_extraction_context(args["source_id"])

    elif cmd == "propose_concept":
        return brain.propose_concept(
            concept_id=args["concept_id"],
            label=args["label"],
            domain_id=args["domain_id"],
            source_id=args["source_id"],
            edges=args.get("edges", []),
        )

    elif cmd == "list_proposals":
        return brain.list_proposals()

    elif cmd == "accept_proposal":
        return brain.accept_proposal(args["concept_id"])

    elif cmd == "reject_proposal":
        return brain.reject_proposal(
            args["concept_id"],
            reason=args.get("reason", ""),
        )

    elif cmd == "bootstrap":
        return bootstrap_workspace()

    elif cmd == "domains_get":
        return brain.list_domains_table()

    elif cmd == "domains_set":
        return brain.upsert_domain(
            label=args["label"],
            keywords=args.get("keywords", ""),
            short_label=args.get("short_label"),
            color=args.get("color"),
            domain_id=args.get("domain_id"),
        )

    elif cmd == "domains_delete":
        return brain.delete_domain(int(args["domain_id"]))

    elif cmd == "axioms_get":
        return brain.list_axioms(active_only=args.get("active_only", True))

    elif cmd == "axioms_revise":
        return brain.revise_axiom(
            key=args["key"],
            body=args["body"],
            citations=args.get("citations"),
            boundary=args.get("boundary"),
        )

    elif cmd == "axioms_history":
        return brain.get_axiom_history(args["key"])

    elif cmd == "ingest_url":
        return brain.ingest_url(
            url=args["url"],
            title=args.get("title"),
            domain_hint=args.get("domain_hint"),
        )

    elif cmd == "ingest_text":
        return brain.ingest_text(
            text=args["text"],
            title=args.get("title"),
            metadata=args.get("metadata"),
            domain_hint=args.get("domain_hint"),
        )

    elif cmd == "reingest":
        return brain.reingest_source(args["source_id"])

    elif cmd == "source_delete":
        return brain.delete_source(args["source_id"])

    elif cmd == "prediction_save":
        return brain.save_prediction(args["model_id"], args["question"], **{
            k: v for k, v in args.items() if k not in ("model_id", "question")
        })

    elif cmd == "prediction_update":
        return brain.update_prediction(args["model_id"], **{
            k: v for k, v in args.items() if k != "model_id"
        })

    elif cmd == "prediction_get":
        return brain.get_prediction(args["model_id"])

    elif cmd == "prediction_list":
        return brain.list_predictions(
            status=args.get("status"),
            quality=args.get("quality"),
        )

    elif cmd == "prediction_ingest_narrative":
        result = brain.ingest_prediction_narrative(
            args["model_id"],
            narrative_path=args.get("narrative_path"),
        )
        return result if result is not None else {"status": "ok", "model_id": args["model_id"]}

    else:
        raise ValueError(f"Unknown command: {cmd}")


def bootstrap_workspace() -> dict:
    """First-run bootstrap: create workspace structure under prism/.

    Idempotent — safe to call on every startup. Only creates what's missing.
    Domains and axioms emerge through ingestion and conversation, and
    prism-axioms.md is regenerated as a read-only projection from the axioms table.

    Also performs a one-time migration of any pre-namespacing layout
    (loose brain.db, AXIOMS.md, GRAPH.json, graph-explorer.html, inbox/, sources/,
    extensions/ at the workspace root) into the new prism/ container.

    Returns a dict describing what was created or migrated.
    """
    workspace = brain.WORKSPACE_ROOT
    created = []

    if not os.access(str(workspace), os.W_OK):
        raise PermissionError(
            f"PRISM needs write access to {workspace} to create your brain. "
            "Please select a folder you have write access to."
        )

    # One-time migration from pre-namespacing layout
    migrated = _migrate_legacy_workspace(workspace)
    created.extend(migrated)

    # Ensure prism/ container exists
    prism_dir = workspace / brain.PRISM_DIR_NAME
    if not prism_dir.exists():
        prism_dir.mkdir(parents=True, exist_ok=True)
        created.append(f"directory: {brain.PRISM_DIR_NAME}/")

    # Children: prism-inbox/, prism-sources/, prism-extensions/
    for sub in ["inbox", "sources", "extensions"]:
        target = prism_dir / f"{brain.PRISM_PREFIX}{sub}"
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(f"directory: {brain.PRISM_DIR_NAME}/{brain.PRISM_PREFIX}{sub}/")

    db_path = brain.DB_EXPORT_PATH
    db_existed = db_path.exists() and db_path.stat().st_size > 0
    if not db_existed:
        conn = brain.init_db()
        conn.close()
        created.append(f"file: {brain.PRISM_DIR_NAME}/{brain.PRISM_PREFIX}brain.db (empty, with schema)")

    if not brain.AXIOMS_MD.exists():
        brain.regenerate_axioms_projection()
        created.append(
            f"file: {brain.PRISM_DIR_NAME}/{brain.PRISM_PREFIX}axioms.md "
            "(read-only projection — edit through Claude)"
        )

    is_new = len(created) > 0
    stats = brain.stats_dict()

    return {
        "is_new": is_new,
        "created": created,
        "workspace": str(workspace),
        "stats": stats,
    }


def _migrate_legacy_workspace(workspace) -> list:
    """Move pre-namespacing layout into prism/ if detected.

    Idempotent: returns an empty list if nothing legacy is present. Otherwise
    moves files/directories into the new container, rewrites stored source
    paths in the DB, and returns a list of human-readable migration entries.
    """
    import shutil
    actions = []

    legacy_files = {
        "brain.db": f"{brain.PRISM_PREFIX}brain.db",
        "brain.db-shm": f"{brain.PRISM_PREFIX}brain.db-shm",
        "brain.db-wal": f"{brain.PRISM_PREFIX}brain.db-wal",
        "AXIOMS.md": f"{brain.PRISM_PREFIX}axioms.md",
        "GRAPH.json": f"{brain.PRISM_PREFIX}graph.json",
        "graph-explorer.html": f"{brain.PRISM_PREFIX}graph-explorer.html",
    }
    legacy_dirs = {
        "inbox": f"{brain.PRISM_PREFIX}inbox",
        "sources": f"{brain.PRISM_PREFIX}sources",
        "extensions": f"{brain.PRISM_PREFIX}extensions",
        "predictions": f"{brain.PRISM_PREFIX}predictions",
    }

    has_legacy = any((workspace / n).exists() for n in legacy_files) or \
                 any((workspace / n).is_dir() for n in legacy_dirs)
    if not has_legacy:
        return actions

    prism_dir = workspace / brain.PRISM_DIR_NAME
    prism_dir.mkdir(parents=True, exist_ok=True)

    for old_name, new_name in legacy_files.items():
        src = workspace / old_name
        dst = prism_dir / new_name
        if src.exists() and not dst.exists():
            shutil.move(str(src), str(dst))
            actions.append(f"migrated: {old_name} -> {brain.PRISM_DIR_NAME}/{new_name}")

    for old_name, new_name in legacy_dirs.items():
        src = workspace / old_name
        dst = prism_dir / new_name
        if src.is_dir() and not dst.exists():
            shutil.move(str(src), str(dst))
            actions.append(f"migrated: {old_name}/ -> {brain.PRISM_DIR_NAME}/{new_name}/")

    # Rewrite stored source paths in DB metadata: "sources/X" -> "prism/prism-sources/X"
    db_path = prism_dir / f"{brain.PRISM_PREFIX}brain.db"
    if db_path.exists():
        try:
            import sqlite3, json as _json
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, metadata FROM nodes WHERE type = 'source' AND metadata IS NOT NULL"
            ).fetchall()
            rewritten = 0
            for r in rows:
                try:
                    meta = _json.loads(r["metadata"]) if r["metadata"] else {}
                except Exception:
                    continue
                p = meta.get("path")
                if not p:
                    continue
                new_p = None
                if p.startswith("sources/"):
                    new_p = f"{brain.PRISM_DIR_NAME}/{brain.PRISM_PREFIX}sources/" + p[len("sources/"):]
                if new_p:
                    meta["path"] = new_p
                    conn.execute(
                        "UPDATE nodes SET metadata = ? WHERE id = ?",
                        (_json.dumps(meta), r["id"]),
                    )
                    rewritten += 1
            if rewritten:
                conn.commit()
                actions.append(f"rewrote {rewritten} source path(s) in DB metadata")
            conn.close()
        except Exception as e:
            actions.append(f"warning: source path rewrite failed: {e}")

    return actions


# ---------------------------------------------------------------------------
# Per-request setup (shared by both modes)
# ---------------------------------------------------------------------------

def _apply_request_env(request: dict) -> None:
    """Configure brain for this request's workspace + embedding backend."""
    if "workspace" in request:
        new_ws = request["workspace"]
        if str(brain.WORKSPACE_ROOT) != str(new_ws):
            brain.configure(workspace_root=new_ws)
            brain.reset_domains_cache()

    backend = os.environ.get("PRISM_EMBEDDING_BACKEND")
    if backend and brain.EMBEDDING_BACKEND != backend:
        brain.EMBEDDING_BACKEND = backend
        brain.reset_embedder()

    # Ensure DB schema exists (cheap on subsequent calls — IF NOT EXISTS).
    conn = brain.init_db()
    conn.close()


def _dispatch_with_io_capture(request: dict) -> dict:
    """Run handle(request) with stdout redirected to stderr (so engine prints
    don't corrupt the JSON response stream). Returns the result envelope.
    """
    rid = request.get("id")
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        result = handle(request)
        envelope = {"ok": True, "result": result}
    except Exception as e:
        envelope = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        sys.stdout = real_stdout
    if rid is not None:
        envelope["id"] = rid
    return envelope


# ---------------------------------------------------------------------------
# Single-shot mode (legacy / subprocess-per-call)
# ---------------------------------------------------------------------------

def _single_shot() -> None:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stdout.write(json.dumps({"ok": False, "error": "Empty request"}) + "\n")
        sys.stdout.flush()
        return

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}) + "\n")
        sys.stdout.flush()
        return

    _load_brain()
    _apply_request_env(request)
    envelope = _dispatch_with_io_capture(request)
    sys.stdout.write(json.dumps(envelope) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Daemon mode (long-running NDJSON loop)
# ---------------------------------------------------------------------------

def _daemon_loop() -> None:
    """Read newline-delimited JSON requests on stdin, write responses on stdout.

    Engine is imported once. Each request runs synchronously to completion
    before the next is read — this matches MCP usage (Claude awaits each tool
    call) and avoids thread-safety questions inside brain.py.

    Unhandled exceptions inside dispatch are caught and returned as error
    envelopes — they never bring down the loop. Anything else (corrupt JSON,
    EOF) is logged to stderr and the loop continues / exits cleanly.
    """
    # Probe heavy runtime deps and run bootstrap.sh if any are missing.
    # See _ensure_runtime_deps for why this is necessary even though
    # `import brain` succeeded.
    _ensure_runtime_deps()
    _load_brain()

    # Announce readiness on stderr so the parent can wait for it.
    print("  [daemon] ready (pid={})".format(os.getpid()), file=sys.stderr)
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(
                json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}) + "\n"
            )
            sys.stdout.flush()
            continue

        try:
            _apply_request_env(request)
            envelope = _dispatch_with_io_capture(request)
        except Exception as e:
            # Setup failure (workspace permissions etc.) — never let it kill
            # the loop.
            envelope = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            rid = request.get("id")
            if rid is not None:
                envelope["id"] = rid

        sys.stdout.write(json.dumps(envelope) + "\n")
        sys.stdout.flush()


def main():
    if "--daemon" in sys.argv:
        _daemon_loop()
    else:
        _single_shot()


if __name__ == "__main__":
    main()
