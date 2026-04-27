#!/usr/bin/env python3
"""
brain.py — PRISM knowledge base engine.

Unified SQLite storage with hybrid search (BM25 keyword + semantic vector).

Usage:
    python3 brain.py ingest
    python3 brain.py search "energy constraints on AI scaling"
    python3 brain.py search --mode keyword "tokenization"
    python3 brain.py search --mode semantic "what happens to human purpose"
    python3 brain.py graph C11
    python3 brain.py graph C11 --hops 2
    python3 brain.py export-graph
    python3 brain.py tag S41 10,12
    python3 brain.py registry
    python3 brain.py stats
"""
from __future__ import annotations

import sqlite3
import json
import struct
import sys
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENGINE_DIR = Path(__file__).parent

# Workspace root: the user-chosen folder. PRISM stores everything inside a
# single prism/ container at the workspace root, with prism- prefixed children.
# Override workspace root via PRISM_WORKSPACE env var or configure().
WORKSPACE_ROOT = Path(os.environ.get("PRISM_WORKSPACE", str(ENGINE_DIR.parent)))

# Container directory name and prefix. Centralised so docs and code agree.
PRISM_DIR_NAME = "prism"
PRISM_PREFIX = "prism-"
PRISM_DIR = WORKSPACE_ROOT / PRISM_DIR_NAME

# Session directory for SQLite operations.
# Cowork VMs need this because FUSE mounts don't support WAL.
# When set, prism-brain.db is copied here at startup and back to workspace after writes.
_session_dir_env = os.environ.get("PRISM_SESSION_DIR", "")
_SESSION_DIR = Path(_session_dir_env) if _session_dir_env else None


def _compute_db_path():
    if _SESSION_DIR and _SESSION_DIR.exists():
        return _SESSION_DIR / f"{PRISM_PREFIX}brain.db"
    return PRISM_DIR / f"{PRISM_PREFIX}brain.db"


DB_PATH = _compute_db_path()
DB_EXPORT_PATH = PRISM_DIR / f"{PRISM_PREFIX}brain.db"
GRAPH_JSON = PRISM_DIR / f"{PRISM_PREFIX}graph.json"
GRAPH_HTML = PRISM_DIR / f"{PRISM_PREFIX}graph-explorer.html"
AXIOMS_MD = PRISM_DIR / f"{PRISM_PREFIX}axioms.md"
INBOX_DIR = PRISM_DIR / f"{PRISM_PREFIX}inbox"
SOURCES_DIR = PRISM_DIR / f"{PRISM_PREFIX}sources"
EXTENSIONS_DIR = PRISM_DIR / f"{PRISM_PREFIX}extensions"
PREDICTIONS_DIR = PRISM_DIR / f"{PRISM_PREFIX}predictions"

# Chunking parameters
CHUNK_SIZE = 512        # target tokens per chunk (approx chars / 4)
CHUNK_OVERLAP = 64      # token overlap between chunks
CHUNK_CHARS = CHUNK_SIZE * 4
OVERLAP_CHARS = CHUNK_OVERLAP * 4

# Embedding model
MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
EMBEDDING_BACKEND = "auto"  # "auto", "sentence-transformers", or "tfidf"

# Search defaults
DEFAULT_TOP_K = 3
RRF_K = 60

# Source file extensions to ingest
INGEST_EXTENSIONS = {".md", ".txt", ".pdf"}

# Large PDF threshold (pages)
LARGE_PDF_BATCH_SIZE = 100

_db_restored = False


_UNSET = object()


def configure(workspace_root=None, session_dir=_UNSET):
    """Configure engine paths. Call before any operations.

    Args:
        workspace_root: Path to workspace directory (the user-chosen folder; PRISM
            keeps its data under <workspace>/prism/).
        session_dir: Path to session directory, or None to clear. Omit to leave unchanged.
    """
    global WORKSPACE_ROOT, PRISM_DIR, _SESSION_DIR, DB_PATH, DB_EXPORT_PATH
    global GRAPH_JSON, GRAPH_HTML, AXIOMS_MD, INBOX_DIR, SOURCES_DIR
    global EXTENSIONS_DIR, PREDICTIONS_DIR, _db_restored
    if workspace_root is not None:
        WORKSPACE_ROOT = Path(workspace_root)
    if session_dir is not _UNSET:
        _SESSION_DIR = Path(session_dir) if session_dir else None
    PRISM_DIR = WORKSPACE_ROOT / PRISM_DIR_NAME
    DB_PATH = _compute_db_path()
    DB_EXPORT_PATH = PRISM_DIR / f"{PRISM_PREFIX}brain.db"
    GRAPH_JSON = PRISM_DIR / f"{PRISM_PREFIX}graph.json"
    GRAPH_HTML = PRISM_DIR / f"{PRISM_PREFIX}graph-explorer.html"
    AXIOMS_MD = PRISM_DIR / f"{PRISM_PREFIX}axioms.md"
    INBOX_DIR = PRISM_DIR / f"{PRISM_PREFIX}inbox"
    SOURCES_DIR = PRISM_DIR / f"{PRISM_PREFIX}sources"
    EXTENSIONS_DIR = PRISM_DIR / f"{PRISM_PREFIX}extensions"
    PREDICTIONS_DIR = PRISM_DIR / f"{PRISM_PREFIX}predictions"
    _db_restored = False


def _restore_db_from_workspace():
    """Restore prism-brain.db from workspace if the session copy is missing/empty."""
    if DB_PATH == DB_EXPORT_PATH:
        return

    session_exists = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    workspace_exists = DB_EXPORT_PATH.exists() and DB_EXPORT_PATH.stat().st_size > 0

    if not session_exists and workspace_exists:
        import shutil
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(DB_EXPORT_PATH), str(DB_PATH))
        print(f"  Restored prism-brain.db from workspace ({DB_EXPORT_PATH.stat().st_size / 1024:.0f} KB)")


def _ensure_db_restored():
    global _db_restored
    if not _db_restored:
        _restore_db_from_workspace()
        _db_restored = True


# ---------------------------------------------------------------------------
# Embedding engine (pluggable: sentence-transformers or TF-IDF fallback)
# ---------------------------------------------------------------------------

_embedder = None


class TFIDFEmbedder:
    """TF-IDF based embeddings using scikit-learn. Always available, no downloads."""

    def __init__(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectorizer = TfidfVectorizer(
            max_features=2048,
            sublinear_tf=True,
            strip_accents="unicode",
            ngram_range=(1, 2),
            min_df=1,
            max_df=0.95,
        )
        self._fitted = False
        self._corpus_vectors = None

    def fit(self, texts: list[str]):
        if not texts:
            return
        if len(texts) < 3:
            self.vectorizer.max_df = 1.0
        else:
            self.vectorizer.max_df = 0.95
        self.vectorizer.fit(texts)
        self._fitted = True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self._fitted:
            self.fit(texts)
        matrix = self.vectorizer.transform(texts)
        from sklearn.preprocessing import normalize
        matrix = normalize(matrix, norm="l2")
        return matrix.toarray().tolist()

    def embed_single(self, text: str) -> list[float]:
        if not self._fitted:
            raise RuntimeError("TF-IDF model not fitted. Run ingest first.")
        matrix = self.vectorizer.transform([text])
        from sklearn.preprocessing import normalize
        matrix = normalize(matrix, norm="l2")
        return matrix.toarray()[0].tolist()

    @property
    def dim(self):
        return self.vectorizer.max_features if self._fitted else 2048


class SentenceTransformerEmbedder:
    """Sentence-transformers embeddings. Requires HuggingFace model download."""

    def __init__(self):
        cache_dir = ENGINE_DIR / ".cache" / "models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["TRANSFORMERS_CACHE"] = str(cache_dir)
        os.environ["HF_HOME"] = str(cache_dir)
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(MODEL_NAME, cache_folder=str(cache_dir))

    def fit(self, texts: list[str]):
        pass

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        embeddings = self.model.encode(texts, show_progress_bar=len(texts) > 50,
                                       normalize_embeddings=True)
        return embeddings.tolist()

    def embed_single(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    @property
    def dim(self):
        return EMBEDDING_DIM


def get_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder

    backend = EMBEDDING_BACKEND

    if backend == "auto":
        try:
            _embedder = SentenceTransformerEmbedder()
            print("  Embedding backend: sentence-transformers")
        except Exception as e:
            print(f"  sentence-transformers unavailable ({type(e).__name__}), using TF-IDF")
            _embedder = TFIDFEmbedder()
            print("  Embedding backend: TF-IDF (scikit-learn)")
    elif backend == "sentence-transformers":
        _embedder = SentenceTransformerEmbedder()
    else:
        _embedder = TFIDFEmbedder()
        print("  Embedding backend: TF-IDF (scikit-learn)")

    return _embedder


def reset_embedder():
    """Reset the embedder (used by tests or when reconfiguring)."""
    global _embedder
    _embedder = None


def embed_texts(texts: list[str]) -> list[list[float]]:
    return get_embedder().embed_texts(texts)


def embed_single(text: str) -> list[float]:
    return get_embedder().embed_single(text)


# ---------------------------------------------------------------------------
# Vector serialization (float32 -> bytes)
# ---------------------------------------------------------------------------

def vec_to_bytes(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_vec(b: bytes) -> list[float]:
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    import numpy as np
    a, b = np.array(a), np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


# ---------------------------------------------------------------------------
# Domain configuration
# ---------------------------------------------------------------------------


_OVERFLOW_COLORS = ["#8e44ad", "#3498db", "#e91e63", "#00bcd4", "#9c27b0", "#009688", "#ff5722", "#607d8b"]

_domains_cache = None


def load_domains() -> list[dict]:
    """Load domain configuration from the `domains` table — the single source of truth.

    Empty table returns an empty list (the engine treats unclassified content
    as legitimately unclassified). There is no file fallback and no hardcoded
    default list. Domains emerge through ingestion (folder hints, prompt
    context) and conversational refinement via `prism_core_domains_set`.

    A `domains.json` left in the workspace from an older install is ignored.
    If you have one, run `prism_core_domains_set` per row to migrate, then delete
    the file.

    Returns list of dicts with keys: id, label, short_label, color, keywords.
    """
    global _domains_cache
    if _domains_cache is not None:
        return _domains_cache

    try:
        table_rows = list_domains_table()
    except Exception:
        table_rows = []

    _domains_cache = table_rows
    return table_rows


def reset_domains_cache():
    """Clear cached domains (used after configure() or in tests)."""
    global _domains_cache
    _domains_cache = None


def get_domain_descriptions() -> dict[int, str]:
    """Return {domain_id: keyword_description} for the classifier."""
    return {d["id"]: d["keywords"] for d in load_domains()}


def get_domain_labels() -> dict[int, str]:
    """Return {domain_id: full_label}."""
    return {d["id"]: d["label"] for d in load_domains()}


def get_domain_short_labels() -> dict[int, str]:
    """Return {domain_id: short_label} for the graph legend."""
    return {d["id"]: d["short_label"] for d in load_domains()}


def get_domain_colors() -> dict[int, str]:
    """Return {domain_id: hex_color}."""
    return {d["id"]: d["color"] for d in load_domains()}


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

SCHEMA = """
-- Graph structure
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('domain', 'concept', 'source', 'prediction')),
    group_id INTEGER,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES nodes(id),
    target_id TEXT NOT NULL REFERENCES nodes(id),
    type TEXT NOT NULL,
    label TEXT,
    UNIQUE(source_id, target_id, type)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);

-- Document chunks for RAG
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES nodes(id),
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    metadata TEXT DEFAULT '{}',
    UNIQUE(source_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);

-- FTS5 for keyword search (BM25)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Vector embeddings (stored as float32 BLOBs)
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id),
    embedding BLOB NOT NULL
);

-- Metadata
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Predictions (51Folds extension — will move to extension in Phase 6)
CREATE TABLE IF NOT EXISTS predictions (
    model_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    model_type TEXT DEFAULT 'Advanced',
    status TEXT NOT NULL DEFAULT 'queued',
    quality TEXT DEFAULT NULL,
    quality_reason TEXT DEFAULT NULL,
    progress INTEGER DEFAULT 0,
    status_label TEXT DEFAULT '',
    short_summary TEXT DEFAULT '',
    outcomes TEXT DEFAULT '[]',
    outcome_probs TEXT DEFAULT NULL,
    edge_stats TEXT DEFAULT NULL,
    driver_count INTEGER DEFAULT 0,
    source_thesis TEXT DEFAULT NULL,
    report_fetched INTEGER DEFAULT 0,
    narrative_generated INTEGER DEFAULT 0,
    ingested_to_brain INTEGER DEFAULT 0,
    added_to_graph INTEGER DEFAULT 0,
    graph_node_id TEXT DEFAULT NULL,
    platform_model_id TEXT DEFAULT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT DEFAULT NULL,
    updated_at TEXT NOT NULL,
    artifacts_path TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_predictions_quality ON predictions(quality);

-- Domains (replaces domains.json — DB is source of truth)
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    short_label TEXT,
    color TEXT,
    keywords TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Axioms: DB is authoritative; prism-axioms.md is a regenerated projection
CREATE TABLE IF NOT EXISTS axioms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    body TEXT NOT NULL,
    citations TEXT DEFAULT '[]',
    boundary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    superseded_by INTEGER REFERENCES axioms(id),
    superseded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_axioms_key ON axioms(key);
CREATE INDEX IF NOT EXISTS idx_axioms_superseded ON axioms(superseded_by);
"""


def init_db() -> sqlite3.Connection:
    """Initialize database with schema."""
    _ensure_db_restored()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def export_db_to_workspace():
    """Copy prism-brain.db to workspace folder for persistence between sessions."""
    import shutil
    if DB_PATH != DB_EXPORT_PATH and DB_PATH.exists():
        DB_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(DB_PATH), str(DB_EXPORT_PATH))
        print(f"  Exported DB to {DB_EXPORT_PATH} ({DB_EXPORT_PATH.stat().st_size / 1024:.0f} KB)")


# ---------------------------------------------------------------------------
# Predictions table CRUD (51Folds extension — preserved for Phase 6 migration)
# ---------------------------------------------------------------------------

def save_prediction(model_id: str, question: str, **kwargs) -> dict:
    conn = init_db()
    now = datetime.now().isoformat()

    outcomes = kwargs.get("outcomes", [])
    if isinstance(outcomes, list):
        outcomes = json.dumps(outcomes)
    outcome_probs = kwargs.get("outcome_probs")
    if isinstance(outcome_probs, dict):
        outcome_probs = json.dumps(outcome_probs)
    edge_stats = kwargs.get("edge_stats")
    if isinstance(edge_stats, dict):
        edge_stats = json.dumps(edge_stats)

    conn.execute("""
        INSERT INTO predictions (
            model_id, question, model_type, status, quality, quality_reason,
            progress, status_label, short_summary, outcomes, outcome_probs,
            edge_stats, driver_count, source_thesis, report_fetched,
            narrative_generated, ingested_to_brain, added_to_graph,
            graph_node_id, platform_model_id, created_at, completed_at,
            updated_at, artifacts_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(model_id) DO UPDATE SET
            question=excluded.question, model_type=excluded.model_type,
            status=excluded.status, quality=excluded.quality,
            quality_reason=excluded.quality_reason, progress=excluded.progress,
            status_label=excluded.status_label, short_summary=excluded.short_summary,
            outcomes=excluded.outcomes, outcome_probs=excluded.outcome_probs,
            edge_stats=excluded.edge_stats, driver_count=excluded.driver_count,
            source_thesis=excluded.source_thesis, report_fetched=excluded.report_fetched,
            narrative_generated=excluded.narrative_generated,
            ingested_to_brain=excluded.ingested_to_brain,
            added_to_graph=excluded.added_to_graph, graph_node_id=excluded.graph_node_id,
            platform_model_id=excluded.platform_model_id,
            completed_at=excluded.completed_at, updated_at=excluded.updated_at,
            artifacts_path=excluded.artifacts_path
    """, (
        model_id, question,
        kwargs.get("model_type", "Advanced"), kwargs.get("status", "queued"),
        kwargs.get("quality"), kwargs.get("quality_reason"),
        kwargs.get("progress", 0), kwargs.get("status_label", ""),
        kwargs.get("short_summary", ""), outcomes, outcome_probs, edge_stats,
        kwargs.get("driver_count", 0), kwargs.get("source_thesis"),
        int(kwargs.get("report_fetched", False)),
        int(kwargs.get("narrative_generated", False)),
        int(kwargs.get("ingested_to_brain", False)),
        int(kwargs.get("added_to_graph", False)),
        kwargs.get("graph_node_id"), kwargs.get("platform_model_id"),
        kwargs.get("created_at", now), kwargs.get("completed_at"),
        now, kwargs.get("artifacts_path"),
    ))
    conn.commit()
    conn.close()
    export_db_to_workspace()
    return get_prediction(model_id)


def update_prediction(model_id: str, **kwargs) -> dict:
    conn = init_db()
    now = datetime.now().isoformat()

    allowed = {
        "question", "model_type", "status", "quality", "quality_reason",
        "progress", "status_label", "short_summary", "outcomes", "outcome_probs",
        "edge_stats", "driver_count", "source_thesis", "report_fetched",
        "narrative_generated", "ingested_to_brain", "added_to_graph",
        "graph_node_id", "platform_model_id", "completed_at", "artifacts_path"
    }

    sets = ["updated_at = ?"]
    values = [now]

    for key, val in kwargs.items():
        if key not in allowed:
            continue
        if key in ("outcomes", "outcome_probs", "edge_stats") and isinstance(val, (dict, list)):
            val = json.dumps(val)
        if key in ("report_fetched", "narrative_generated", "ingested_to_brain", "added_to_graph"):
            val = int(val)
        sets.append(f"{key} = ?")
        values.append(val)

    values.append(model_id)
    sql = f"UPDATE predictions SET {', '.join(sets)} WHERE model_id = ?"
    conn.execute(sql, values)
    conn.commit()
    conn.close()
    export_db_to_workspace()
    return get_prediction(model_id)


def get_prediction(model_id: str) -> Optional[dict]:
    conn = init_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM predictions WHERE model_id = ?", (model_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    for key in ("outcomes", "outcome_probs", "edge_stats"):
        if d.get(key) and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    for key in ("report_fetched", "narrative_generated", "ingested_to_brain", "added_to_graph"):
        d[key] = bool(d.get(key, 0))
    return d


def list_predictions(status: str = None, quality: str = None) -> list[dict]:
    conn = init_db()
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM predictions WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if quality:
        sql += " AND quality = ?"
        params.append(quality)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        for key in ("outcomes", "outcome_probs", "edge_stats"):
            if d.get(key) and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        for key in ("report_fetched", "narrative_generated", "ingested_to_brain", "added_to_graph"):
            d[key] = bool(d.get(key, 0))
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# Domains table CRUD (DB is source of truth; domains.json is legacy fallback)
# ---------------------------------------------------------------------------

_DOMAIN_FALLBACK_COLORS = ["#4ecdc4", "#ff6b6b", "#a66cff", "#45b7d1", "#f7dc6f",
                            "#e74c3c", "#2ecc71", "#7f8c8d", "#d35400", "#1abc9c",
                            "#e67e22", "#f39c12", "#8e44ad", "#3498db", "#e91e63",
                            "#00bcd4", "#9c27b0", "#009688", "#ff5722", "#607d8b"]


def list_domains_table() -> list[dict]:
    """Return all domains from the `domains` table (id, label, short_label, color, keywords)."""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM domains ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_domain(label: str, keywords: str = "", short_label: Optional[str] = None,
                  color: Optional[str] = None, domain_id: Optional[int] = None) -> dict:
    """Insert a new domain or update an existing one.

    If domain_id is provided, updates that row. Otherwise:
      - If a domain with this label already exists (case-insensitive), updates it
      - Else inserts a new row with the next available id
    Returns the domain dict.
    """
    conn = init_db()
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()

    short_label = short_label or label
    if not color:
        existing_count = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
        color = _DOMAIN_FALLBACK_COLORS[existing_count % len(_DOMAIN_FALLBACK_COLORS)]

    if domain_id is None:
        match = conn.execute(
            "SELECT id FROM domains WHERE LOWER(label) = LOWER(?)", (label,)
        ).fetchone()
        if match:
            domain_id = match["id"]

    if domain_id is not None:
        conn.execute("""
            UPDATE domains SET label = ?, short_label = ?, color = ?, keywords = ?,
                               updated_at = ? WHERE id = ?
        """, (label, short_label, color, keywords, now, domain_id))
    else:
        cur = conn.execute("""
            INSERT INTO domains (label, short_label, color, keywords, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (label, short_label, color, keywords, now, now))
        domain_id = cur.lastrowid

    conn.commit()
    row = conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
    conn.close()
    reset_domains_cache()
    export_db_to_workspace()
    return dict(row) if row else None


def delete_domain(domain_id: int) -> dict:
    """Delete a domain row. Does not cascade to graph nodes (they keep group_id)."""
    conn = init_db()
    cur = conn.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    reset_domains_cache()
    export_db_to_workspace()
    return {"deleted": deleted, "domain_id": domain_id}


def seed_domain_from_folder(folder_name: str) -> dict:
    """Create a domain row from an inbox subfolder name, if it doesn't exist."""
    label = folder_name.replace("-", " ").replace("_", " ").strip().title()
    if not label:
        return None
    return upsert_domain(label=label, keywords=label.lower())


# ---------------------------------------------------------------------------
# Axioms table CRUD (DB is source of truth; prism-axioms.md is a regenerated projection)
# ---------------------------------------------------------------------------

def list_axioms(active_only: bool = True) -> list[dict]:
    """Return axioms. By default, only active (not superseded)."""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM axioms"
    if active_only:
        sql += " WHERE superseded_by IS NULL"
    sql += " ORDER BY key, created_at"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [_axiom_row_to_dict(r) for r in rows]


def get_axiom(key: str, active_only: bool = True) -> Optional[dict]:
    """Return the active axiom for a given key, or None."""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM axioms WHERE key = ?"
    if active_only:
        sql += " AND superseded_by IS NULL"
    sql += " ORDER BY created_at DESC LIMIT 1"
    row = conn.execute(sql, (key,)).fetchone()
    conn.close()
    return _axiom_row_to_dict(row) if row else None


def revise_axiom(key: str, body: str, citations: Optional[list] = None,
                 boundary: Optional[str] = None) -> dict:
    """Add a new axiom row, marking any prior active row for the same key as superseded.

    Returns the new active axiom dict. prism-axioms.md projection is regenerated.

    Citations are validated against existing source nodes — unknown ids are
    not rejected (the user may legitimately cite material they're about to
    ingest), but the returned dict includes an `unknown_citations` list so
    the caller (or `prism-companion` coaching) can surface them. This is
    an engine-side backstop against hallucinated source ids.
    """
    conn = init_db()
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()
    citations = citations or []
    citations_json = json.dumps(citations)

    unknown_citations = []
    if citations:
        existing = {
            r[0] for r in conn.execute(
                "SELECT id FROM nodes WHERE type = 'source'"
            ).fetchall()
        }
        unknown_citations = [c for c in citations if c not in existing]

    cur = conn.execute("""
        INSERT INTO axioms (key, body, citations, boundary, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (key, body, citations_json, boundary, now, now))
    new_id = cur.lastrowid

    conn.execute("""
        UPDATE axioms SET superseded_by = ?, superseded_at = ?
        WHERE key = ? AND id != ? AND superseded_by IS NULL
    """, (new_id, now, key, new_id))

    conn.commit()
    row = conn.execute("SELECT * FROM axioms WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    export_db_to_workspace()
    regenerate_axioms_projection()

    result = _axiom_row_to_dict(row)
    if unknown_citations:
        result["unknown_citations"] = unknown_citations
    return result


def get_axiom_history(key: str) -> list[dict]:
    """Return all axiom revisions for a key, oldest first."""
    conn = init_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM axioms WHERE key = ? ORDER BY created_at", (key,)
    ).fetchall()
    conn.close()
    return [_axiom_row_to_dict(r) for r in rows]


def _axiom_row_to_dict(row) -> dict:
    if row is None:
        return None
    d = dict(row)
    if d.get("citations") and isinstance(d["citations"], str):
        try:
            d["citations"] = json.loads(d["citations"])
        except (json.JSONDecodeError, TypeError):
            d["citations"] = []
    return d


def regenerate_axioms_projection():
    """Regenerate prism-axioms.md as a read-only markdown view of the active axioms.

    This file is a projection — edit through Claude (prism_core_axioms_revise),
    not by hand. Hand-edits will be overwritten on the next revision.
    """
    axioms = list_axioms(active_only=True)
    lines = [
        "# Axioms",
        "",
        "*Generated from `prism-brain.db`. Edit through Claude — do not hand-edit this file.*",
        "",
    ]

    if not axioms:
        lines.append("_No axioms recorded yet. Talk to Claude about what you've been reading; axioms emerge through conversation._")
    else:
        by_key = {}
        for a in axioms:
            by_key.setdefault(a["key"], []).append(a)

        for key in sorted(by_key.keys()):
            entries = by_key[key]
            for a in entries:
                lines.append(f"## {a['key']}")
                lines.append("")
                lines.append(a["body"])
                lines.append("")
                if a.get("boundary"):
                    lines.append(f"**Boundary.** {a['boundary']}")
                    lines.append("")
                if a.get("citations"):
                    cites = ", ".join(a["citations"]) if isinstance(a["citations"], list) else str(a["citations"])
                    lines.append(f"*Citations:* {cites}")
                    lines.append("")

        history = []
        conn = init_db()
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT key, created_at, superseded_at FROM axioms WHERE superseded_by IS NOT NULL ORDER BY superseded_at"
        ).fetchall():
            history.append(f"- `{r['key']}` revised at {r['superseded_at']}")
        conn.close()

        if history:
            lines.append("---")
            lines.append("")
            lines.append("## Revision log")
            lines.append("")
            lines.extend(history)
            lines.append("")

    AXIOMS_MD.parent.mkdir(parents=True, exist_ok=True)
    AXIOMS_MD.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Document chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_chars: int = CHUNK_CHARS,
               overlap_chars: int = OVERLAP_CHARS) -> list[dict]:
    """Split text into overlapping chunks."""
    sections = re.split(r'\n-{3,}\n', text.strip())
    if len(sections) == 1:
        sections = re.split(r'\n{2,}', text.strip())

    fine_sections = []
    for section in sections:
        section = section.strip()
        if not section:
            continue
        if len(section) <= chunk_chars:
            fine_sections.append(section)
        else:
            lines = section.split('\n')
            sub = ""
            for line in lines:
                if len(sub) + len(line) + 1 > chunk_chars and sub:
                    fine_sections.append(sub.strip())
                    sub = line
                else:
                    sub = (sub + "\n" + line) if sub else line
            if sub.strip():
                fine_sections.append(sub.strip())

    chunks = []
    current = ""
    char_pos = 0

    for section in fine_sections:
        if len(current) + len(section) + 2 > chunk_chars and current:
            chunks.append({
                "content": current.strip(),
                "char_start": char_pos - len(current),
                "char_end": char_pos
            })
            if overlap_chars > 0 and len(current) > overlap_chars:
                overlap_text = current[-overlap_chars:]
                current = overlap_text + "\n\n" + section
            else:
                current = section
        else:
            current = (current + "\n\n" + section) if current else section

        char_pos += len(section) + 2

    if current.strip():
        chunks.append({
            "content": current.strip(),
            "char_start": char_pos - len(current),
            "char_end": char_pos
        })

    return chunks


# ---------------------------------------------------------------------------
# PDF extraction with large file batching
# ---------------------------------------------------------------------------

def _extract_pdf_text(path: Path) -> str:
    """Extract text from a PDF, batching large files (100+ pages)."""
    import subprocess

    page_count = _get_pdf_page_count(path)

    if page_count is not None and page_count > LARGE_PDF_BATCH_SIZE:
        print(f"    Large PDF ({page_count} pages), extracting in batches...")
        return _extract_pdf_batched(path, page_count)

    try:
        result = subprocess.run(
            ["pdftotext", str(path), "-"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        from pdfminer.high_level import extract_text
        return extract_text(str(path))
    except ImportError:
        pass
    print(f"  WARNING: Could not extract text from PDF: {path}")
    return ""


def _get_pdf_page_count(path: Path) -> Optional[int]:
    """Get page count of a PDF using pdfinfo."""
    import subprocess
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.split('\n'):
            if line.startswith('Pages:'):
                return int(line.split(':')[1].strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _extract_pdf_batched(path: Path, page_count: int) -> str:
    """Extract text from a large PDF in batches."""
    import subprocess
    text_parts = []
    for start in range(1, page_count + 1, LARGE_PDF_BATCH_SIZE):
        end = min(start + LARGE_PDF_BATCH_SIZE - 1, page_count)
        try:
            result = subprocess.run(
                ["pdftotext", "-f", str(start), "-l", str(end), str(path), "-"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                text_parts.append(result.stdout)
            print(f"      Extracted pages {start}-{end}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            print(f"      Warning: failed to extract pages {start}-{end}")
    return "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Source file management
# ---------------------------------------------------------------------------

def find_source_files() -> list[Path]:
    """Find all ingestible source files in the prism/prism-sources/ directory."""
    if not SOURCES_DIR.exists():
        return []
    files = []
    for path in SOURCES_DIR.rglob("*"):
        if path.is_dir():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() in INGEST_EXTENSIONS:
            files.append(path)
    return sorted(files)


def _read_source_text(path: Path) -> str:
    """Read text from a source file (PDF or text)."""
    if path.suffix.lower() == ".pdf":
        return _extract_pdf_text(path)
    return path.read_text(errors="replace")


def file_to_source_id(path: Path) -> str:
    """Generate a stable source ID from file path."""
    stem = path.stem
    m = re.match(r'^(S\d{2,})-', stem)
    if m:
        return m.group(1)
    h = hashlib.md5(str(path.name).encode()).hexdigest()[:6]
    clean = re.sub(r'[^a-zA-Z0-9]', '-', stem)[:20]
    return f"S_{clean}_{h}"


def next_source_id() -> str:
    """Get the next available source ID (S14, S15, ...) based on files in prism-sources/."""
    existing_nums = []
    if SOURCES_DIR.exists():
        for f in SOURCES_DIR.iterdir():
            m = re.match(r'^S(\d{2,})-', f.stem)
            if m:
                existing_nums.append(int(m.group(1)))
    next_num = max(existing_nums, default=0) + 1
    return f"S{next_num:02d}"


# ---------------------------------------------------------------------------
# Graph import from prism-graph.json (one-time migration)
# ---------------------------------------------------------------------------

def ingest_graph_json(conn: sqlite3.Connection):
    """Import nodes and edges from prism-graph.json into SQLite (one-time migration)."""
    n_existing = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    if n_existing > 0:
        print(f"  Graph already in DB ({n_existing} nodes), skipping prism-graph.json import")
        return

    if not GRAPH_JSON.exists():
        print("  prism-graph.json not found and DB is empty — no graph data available")
        return

    data = json.loads(GRAPH_JSON.read_text())

    for node in data["nodes"]:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, label, type, group_id, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (node["id"], node["label"], node["type"], node.get("group"),
             json.dumps({k: v for k, v in node.items()
                        if k not in ("id", "label", "type", "group")}))
        )

    for edge in data["edges"]:
        conn.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, type, label) "
            "VALUES (?, ?, ?, ?)",
            (edge["source"], edge["target"], edge["type"], edge.get("label"))
        )

    conn.commit()
    print(f"  Migrated from prism-graph.json: {len(data['nodes'])} nodes, {len(data['edges'])} edges")


# ---------------------------------------------------------------------------
# Graph mutation API
# ---------------------------------------------------------------------------

def add_node(node_id: str, label: str, node_type: str, group_id: int = None,
             metadata: dict = None, conn: sqlite3.Connection = None) -> bool:
    """Add or update a node in the graph. Returns True if inserted, False if updated."""
    close = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close = True

    existing = conn.execute("SELECT id FROM nodes WHERE id=?", (node_id,)).fetchone()
    meta_json = json.dumps(metadata or {})

    conn.execute(
        "INSERT OR REPLACE INTO nodes (id, label, type, group_id, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (node_id, label, node_type, group_id, meta_json)
    )
    conn.commit()

    if close:
        conn.close()
    return existing is None


def add_edge(source_id: str, target_id: str, edge_type: str,
             label: str = None, conn: sqlite3.Connection = None) -> bool:
    """Add an edge to the graph. Returns True if inserted, False if already existed."""
    close = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close = True

    try:
        changes_before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, type, label) "
            "VALUES (?, ?, ?, ?)",
            (source_id, target_id, edge_type, label)
        )
        inserted = conn.total_changes > changes_before
        conn.commit()
    except sqlite3.IntegrityError:
        inserted = False

    if close:
        conn.close()
    return inserted


def get_graph_data() -> dict:
    """Read the full graph from the database."""
    conn = sqlite3.connect(str(DB_PATH))

    nodes = conn.execute("SELECT id, label, type, group_id, metadata FROM nodes").fetchall()
    edges = conn.execute("SELECT source_id, target_id, type, label FROM edges").fetchall()

    last_ingest = conn.execute(
        "SELECT value FROM meta WHERE key='last_ingest'").fetchone()

    data = {
        "meta": {
            "title": "PRISM — Knowledge Graph",
            "updated": last_ingest[0] if last_ingest else datetime.now().isoformat(),
            "version": "2.0",
            "nodeCount": len(nodes),
            "edgeCount": len(edges)
        },
        "nodes": [],
        "edges": []
    }

    for n in nodes:
        node = {"id": n[0], "label": n[1], "type": n[2], "group": n[3]}
        meta = json.loads(n[4]) if n[4] else {}
        for k, v in meta.items():
            node[k] = v
        data["nodes"].append(node)

    for e in edges:
        edge = {"source": e[0], "target": e[1], "type": e[2]}
        if e[3]:
            edge["label"] = e[3]
        data["edges"].append(edge)

    conn.close()
    return data


def next_prediction_node_id(conn: sqlite3.Connection = None) -> str:
    """Get the next available prediction node ID (P01, P02, ...)."""
    close = False
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close = True

    rows = conn.execute(
        "SELECT id FROM nodes WHERE id LIKE 'P%' AND type='prediction'"
    ).fetchall()

    existing_nums = []
    for r in rows:
        try:
            existing_nums.append(int(r[0][1:]))
        except ValueError:
            pass

    next_num = max(existing_nums, default=0) + 1
    node_id = f"P{next_num:02d}"

    if close:
        conn.close()
    return node_id


# ---------------------------------------------------------------------------
# Domain classifier
# ---------------------------------------------------------------------------

def classify_source_domains(source_id: str, conn: sqlite3.Connection,
                            top_n: int = 2, threshold: float = 0.05) -> list[dict]:
    """Auto-classify a source into domains using TF-IDF cosine similarity.

    Returns list of {domain_id, score} sorted by score descending.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine

    domain_descriptions = get_domain_descriptions()

    chunks = conn.execute(
        "SELECT content FROM chunks WHERE source_id=?", (source_id,)
    ).fetchall()
    if not chunks:
        return []

    source_text = " ".join(c[0] for c in chunks)

    domain_ids = sorted(domain_descriptions.keys())
    corpus = [domain_descriptions[d] for d in domain_ids] + [source_text]

    vectorizer = TfidfVectorizer(stop_words='english', max_features=5000)
    tfidf_matrix = vectorizer.fit_transform(corpus)

    source_vec = tfidf_matrix[-1]
    domain_vecs = tfidf_matrix[:-1]
    similarities = sklearn_cosine(source_vec, domain_vecs).flatten()

    scored = sorted(zip(domain_ids, similarities), key=lambda x: -x[1])
    result = [{"domain_id": d, "score": float(s)} for d, s in scored[:top_n] if s >= threshold]

    if not result and scored and scored[0][1] > 0:
        result = [{"domain_id": scored[0][0], "score": float(scored[0][1])}]

    return result


def wire_source_to_domains(source_id: str, domain_nums: list[int],
                           conn: sqlite3.Connection) -> list[str]:
    """Wire a source node to domain nodes. Creates domain nodes if missing."""
    actions = []
    labels = get_domain_labels()

    for d in domain_nums:
        domain_node_id = f"D{d}" if d != 11 else "D_PRED"

        existing = conn.execute(
            "SELECT id FROM nodes WHERE id=?", (domain_node_id,)
        ).fetchone()
        if not existing:
            label = labels.get(d, f"Domain {d}")
            conn.execute(
                "INSERT INTO nodes (id, label, type, group_id, metadata) "
                "VALUES (?, ?, 'domain', ?, '{}')",
                (domain_node_id, label, d)
            )
            actions.append(f"Created domain node {domain_node_id} ({label})")

        existing_edge = conn.execute(
            "SELECT 1 FROM edges WHERE source_id=? AND target_id=?",
            (domain_node_id, source_id)
        ).fetchone()
        if not existing_edge:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, label) "
                "VALUES (?, ?, 'contains', NULL)",
                (domain_node_id, source_id)
            )
            actions.append(f"Wired {source_id} -> Domain {d}")

    if domain_nums:
        conn.execute(
            "UPDATE nodes SET group_id=? WHERE id=?",
            (domain_nums[0], source_id)
        )

    conn.commit()
    return actions


# ---------------------------------------------------------------------------
# Tagging (manual domain correction)
# ---------------------------------------------------------------------------

def tag_source(source_id: str, domain_nums: list[int]):
    """Manual domain tagging. Wires graph edges, exports graph."""
    conn = sqlite3.connect(str(DB_PATH))

    existing = conn.execute(
        "SELECT id, label FROM nodes WHERE id=?", (source_id,)
    ).fetchone()
    if not existing:
        print(f"Error: source {source_id} not found in graph")
        conn.close()
        return

    print(f"Tagging {source_id} ({existing[1]}) -> domains {domain_nums}")

    actions = wire_source_to_domains(source_id, domain_nums, conn)
    for a in actions:
        print(f"  {a}")

    conn.close()

    export_db_to_workspace()
    export_graph_json()
    print(f"Tag complete.")


# ---------------------------------------------------------------------------
# LLM concept extraction hooks
# ---------------------------------------------------------------------------

_concept_extraction_hook: Optional[Callable] = None


def set_concept_extraction_hook(hook: Optional[Callable]):
    """Set a callback for LLM concept extraction during ingestion.

    The hook is called after chunking/embedding, before graph wiring, with:
        hook(source_id: str, chunk_texts: list[str], conn: sqlite3.Connection)

    The MCP server fills this in with a function that calls Claude.
    """
    global _concept_extraction_hook
    _concept_extraction_hook = hook


# ---------------------------------------------------------------------------
# Concept proposals and extraction context
# ---------------------------------------------------------------------------

def next_concept_id(conn: sqlite3.Connection = None) -> str:
    """Get the next available concept node ID (C01, C02, ...)."""
    close = False
    if conn is None:
        conn = init_db()
        close = True

    rows = conn.execute(
        "SELECT id FROM nodes WHERE id LIKE 'C%' AND type='concept'"
    ).fetchall()

    existing_nums = []
    for r in rows:
        m = re.match(r'^C(\d+)$', r[0])
        if m:
            existing_nums.append(int(m.group(1)))

    next_num = max(existing_nums, default=0) + 1
    result = f"C{next_num:02d}"

    if close:
        conn.close()
    return result


def get_extraction_context(source_id: str, conn: sqlite3.Connection = None) -> dict:
    """Build context for LLM concept extraction.

    Returns chunks from the source plus the existing graph structure,
    giving Claude everything needed to identify concepts and propose edges.
    """
    close = False
    if conn is None:
        conn = init_db()
        close = True

    chunks = conn.execute(
        "SELECT chunk_index, content FROM chunks WHERE source_id=? ORDER BY chunk_index",
        (source_id,),
    ).fetchall()

    source_node = conn.execute(
        "SELECT label, group_id FROM nodes WHERE id=?", (source_id,)
    ).fetchone()

    existing_concepts = conn.execute(
        "SELECT id, label, group_id FROM nodes WHERE type='concept' ORDER BY id"
    ).fetchall()

    existing_domains = conn.execute(
        "SELECT id, label, group_id FROM nodes WHERE type='domain' ORDER BY id"
    ).fetchall()

    edge_types_used = conn.execute(
        "SELECT DISTINCT type FROM edges ORDER BY type"
    ).fetchall()

    corrections = conn.execute(
        "SELECT value FROM meta WHERE key='correction_log'"
    ).fetchone()
    correction_log = json.loads(corrections[0]) if corrections else []

    result = {
        "source_id": source_id,
        "source_title": source_node[0] if source_node else source_id,
        "source_domain": source_node[1] if source_node else None,
        "chunks": [{"index": c[0], "content": c[1]} for c in chunks],
        "existing_concepts": [
            {"id": c[0], "label": c[1], "domain": c[2]} for c in existing_concepts
        ],
        "existing_domains": [
            {"id": d[0], "label": d[1], "group_id": d[2]} for d in existing_domains
        ],
        "edge_types": [e[0] for e in edge_types_used],
        "recent_corrections": correction_log[-10:],
        "next_concept_id": next_concept_id(conn),
    }

    if close:
        conn.close()
    return result


def propose_concept(concept_id: str, label: str, domain_id: int,
                    source_id: str, edges: list[dict] = None,
                    conn: sqlite3.Connection = None) -> dict:
    """Create a proposed concept node and its edges.

    Proposed concepts have metadata.status = "proposed". They become permanent
    when accepted via accept_proposal().

    Args:
        concept_id: e.g. "C15"
        label: concept name
        domain_id: domain group number
        source_id: source that generated this proposal
        edges: list of {"target": "C12", "type": "relates_to", "label": "..."} dicts
    """
    close = False
    if conn is None:
        conn = init_db()
        close = True

    meta = {"status": "proposed", "proposed_from": source_id}
    add_node(concept_id, label, "concept", group_id=domain_id,
             metadata=meta, conn=conn)

    add_edge(source_id, concept_id, "sourced_from",
             label=f"extracted from {source_id}", conn=conn)

    domain_node_id = f"D{domain_id}" if domain_id != 11 else "D_PRED"
    existing_domain = conn.execute(
        "SELECT id FROM nodes WHERE id=?", (domain_node_id,)
    ).fetchone()
    if existing_domain:
        add_edge(domain_node_id, concept_id, "contains", conn=conn)

    edge_results = []
    for e in (edges or []):
        target = e.get("target", "")
        target_exists = conn.execute(
            "SELECT id FROM nodes WHERE id=?", (target,)
        ).fetchone()
        if target_exists:
            added = add_edge(concept_id, target, e.get("type", "relates_to"),
                           label=e.get("label"), conn=conn)
            edge_results.append({"target": target, "type": e["type"], "added": added})

    if close:
        conn.close()

    return {
        "concept_id": concept_id,
        "label": label,
        "domain_id": domain_id,
        "source_id": source_id,
        "edges_created": edge_results,
        "status": "proposed",
    }


def list_proposals(conn: sqlite3.Connection = None) -> list[dict]:
    """List all proposed (not yet accepted) concept nodes."""
    close = False
    if conn is None:
        conn = init_db()
        close = True

    rows = conn.execute(
        "SELECT id, label, group_id, metadata FROM nodes WHERE type='concept'"
    ).fetchall()

    proposals = []
    for r in rows:
        meta = json.loads(r[3]) if r[3] else {}
        if meta.get("status") != "proposed":
            continue

        edges = conn.execute(
            "SELECT target_id, type, label FROM edges WHERE source_id=?", (r[0],)
        ).fetchall()
        incoming = conn.execute(
            "SELECT source_id, type, label FROM edges WHERE target_id=?", (r[0],)
        ).fetchall()

        proposals.append({
            "id": r[0],
            "label": r[1],
            "domain": r[2],
            "proposed_from": meta.get("proposed_from"),
            "edges_out": [{"target": e[0], "type": e[1], "label": e[2]} for e in edges],
            "edges_in": [{"source": e[0], "type": e[1], "label": e[2]} for e in incoming],
        })

    if close:
        conn.close()
    return proposals


def accept_proposal(concept_id: str, conn: sqlite3.Connection = None) -> dict:
    """Accept a proposed concept, making it permanent."""
    close = False
    if conn is None:
        conn = init_db()
        close = True

    row = conn.execute(
        "SELECT id, label, metadata FROM nodes WHERE id=? AND type='concept'",
        (concept_id,),
    ).fetchone()
    if not row:
        if close:
            conn.close()
        raise ValueError(f"Concept {concept_id} not found")

    meta = json.loads(row[2]) if row[2] else {}
    meta.pop("status", None)
    meta["accepted_at"] = datetime.now().isoformat()
    conn.execute(
        "UPDATE nodes SET metadata=? WHERE id=?",
        (json.dumps(meta), concept_id),
    )
    conn.commit()

    if close:
        conn.close()
    return {"concept_id": concept_id, "label": row[1], "status": "accepted"}


def reject_proposal(concept_id: str, reason: str = "",
                    conn: sqlite3.Connection = None) -> dict:
    """Reject a proposed concept: delete it and log the correction."""
    close = False
    if conn is None:
        conn = init_db()
        close = True

    row = conn.execute(
        "SELECT id, label, group_id, metadata FROM nodes WHERE id=? AND type='concept'",
        (concept_id,),
    ).fetchone()
    if not row:
        if close:
            conn.close()
        raise ValueError(f"Concept {concept_id} not found")

    label = row[1]
    meta = json.loads(row[3]) if row[3] else {}

    conn.execute("DELETE FROM edges WHERE source_id=? OR target_id=?",
                (concept_id, concept_id))
    conn.execute("DELETE FROM nodes WHERE id=?", (concept_id,))
    conn.commit()

    log_correction({
        "action": "reject_concept",
        "concept_id": concept_id,
        "label": label,
        "domain": row[2],
        "proposed_from": meta.get("proposed_from"),
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
    }, conn)

    if close:
        conn.close()
    return {"concept_id": concept_id, "label": label, "status": "rejected"}


def log_correction(entry: dict, conn: sqlite3.Connection = None):
    """Append a correction to the correction log in the meta table."""
    close = False
    if conn is None:
        conn = init_db()
        close = True

    existing = conn.execute(
        "SELECT value FROM meta WHERE key='correction_log'"
    ).fetchone()
    log = json.loads(existing[0]) if existing else []
    log.append(entry)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('correction_log', ?)",
        (json.dumps(log),),
    )
    conn.commit()

    if close:
        conn.close()


# ---------------------------------------------------------------------------
# Ingestion — transaction-per-source
# ---------------------------------------------------------------------------

def _ingest_single_source(conn: sqlite3.Connection, path: Path,
                          source_id: str, chunks: list[dict],
                          embedder) -> dict:
    """Ingest one source file within a savepoint. Rolls back on failure.

    Returns dict with source_id, path, chunk_count on success.
    Raises on failure (caller should catch and continue).
    """
    conn.execute("SAVEPOINT ingest_source")
    try:
        existing = conn.execute("SELECT id FROM nodes WHERE id=?", (source_id,)).fetchone()
        if not existing:
            try:
                rel = str(path.relative_to(WORKSPACE_ROOT))
            except ValueError:
                rel = path.name
            conn.execute(
                "INSERT INTO nodes (id, label, type, group_id, metadata) "
                "VALUES (?, ?, 'source', NULL, ?)",
                (source_id, path.stem, json.dumps({"path": rel}))
            )

        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM chunks WHERE source_id=?", (source_id,)).fetchall()]
        if old_ids:
            placeholders = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM embeddings WHERE chunk_id IN ({placeholders})", old_ids)
            conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))

        chunk_ids = []
        chunk_texts = []
        for i, chunk in enumerate(chunks):
            content_hash = hashlib.sha256(chunk["content"][:500].encode()).hexdigest()
            conn.execute(
                "INSERT INTO chunks (source_id, chunk_index, content, content_hash, "
                "char_start, char_end) VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, i, chunk["content"], content_hash,
                 chunk["char_start"], chunk["char_end"])
            )
            chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            chunk_ids.append(chunk_id)
            chunk_texts.append(chunk["content"])

        if chunk_texts and embedder:
            vecs = embedder.embed_texts(chunk_texts)
            for cid, vec in zip(chunk_ids, vecs):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (chunk_id, embedding) VALUES (?, ?)",
                    (cid, vec_to_bytes(vec))
                )

        if _concept_extraction_hook and chunk_texts:
            _concept_extraction_hook(source_id, chunk_texts, conn)

        conn.execute("RELEASE SAVEPOINT ingest_source")
        print(f"    {path.name}: {len(chunk_ids)} chunks")
        return {"source_id": source_id, "path": str(path), "chunk_count": len(chunk_ids)}

    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT ingest_source")
        raise


def ingest_documents(conn: sqlite3.Connection):
    """Chunk and embed all source documents (full re-ingestion)."""
    files = find_source_files()
    print(f"  Found {len(files)} source files")

    # Phase 1: Read and chunk all files, collect texts for embedder fitting
    source_data = []
    all_chunk_texts = []

    for path in files:
        source_id = file_to_source_id(path)
        text = _read_source_text(path)
        chunks = chunk_text(text)
        source_data.append({"path": path, "source_id": source_id, "chunks": chunks})
        all_chunk_texts.extend(c["content"] for c in chunks)

    # Phase 2: Fit embedder on full corpus
    embedder = get_embedder()
    if all_chunk_texts:
        print(f"  Fitting embedder on {len(all_chunk_texts)} chunks...")
        embedder.fit(all_chunk_texts)

    # Phase 3: Ingest each source transactionally
    succeeded = 0
    failed = 0
    for sd in source_data:
        try:
            _ingest_single_source(conn, sd["path"], sd["source_id"], sd["chunks"], embedder)
            succeeded += 1
        except Exception as e:
            print(f"    ERROR: {sd['path'].name}: {e}")
            failed += 1

    conn.commit()

    # Update meta
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_ingest', datetime('now'))")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('chunk_count', ?)",
                (str(len(all_chunk_texts)),))
    conn.commit()

    if failed:
        print(f"  Ingestion: {succeeded} succeeded, {failed} failed")


def ingest_new_sources(conn: sqlite3.Connection, paths: list[Path]) -> list[dict]:
    """Ingest specific source files (used by inbox processing).

    Fits embedder on existing corpus + new texts for TF-IDF accuracy.
    Returns list of result dicts for successfully ingested sources.
    """
    # Get existing corpus for embedder fitting
    existing_texts = [r[0] for r in conn.execute("SELECT content FROM chunks").fetchall()]

    # Read and chunk new files
    source_data = []
    new_texts = []
    for path in paths:
        source_id = file_to_source_id(path)
        text = _read_source_text(path)
        chunks = chunk_text(text)
        source_data.append({"path": path, "source_id": source_id, "chunks": chunks})
        new_texts.extend(c["content"] for c in chunks)

    # Fit embedder on full corpus
    embedder = get_embedder()
    all_texts = existing_texts + new_texts
    if all_texts:
        embedder.fit(all_texts)

    # Ingest each transactionally
    results = []
    for sd in source_data:
        try:
            result = _ingest_single_source(conn, sd["path"], sd["source_id"], sd["chunks"], embedder)
            results.append(result)
        except Exception as e:
            print(f"  ERROR: {sd['path'].name}: {e}")

    conn.commit()
    return results


# ---------------------------------------------------------------------------
# Stats snapshot
# ---------------------------------------------------------------------------

def _save_stats_snapshot(conn: sqlite3.Connection):
    """Save a timestamped stats snapshot to the meta table for trend tracking."""
    n_sources = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='source'").fetchone()[0]
    n_concepts = conn.execute("SELECT COUNT(*) FROM nodes WHERE type='concept'").fetchone()[0]
    n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_embeds = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    cross_domain = conn.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN nodes n1 ON n1.id = e.source_id
        JOIN nodes n2 ON n2.id = e.target_id
        WHERE n1.group_id IS NOT NULL AND n2.group_id IS NOT NULL
          AND n1.group_id != n2.group_id
    """).fetchone()[0]

    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "sources": n_sources,
        "concepts": n_concepts,
        "nodes": n_nodes,
        "edges": n_edges,
        "chunks": n_chunks,
        "embeddings": n_embeds,
        "cross_domain_edges": cross_domain,
    }

    existing = conn.execute("SELECT value FROM meta WHERE key='stats_history'").fetchone()
    history = json.loads(existing[0]) if existing else []
    history.append(snapshot)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('stats_history', ?)",
                (json.dumps(history),))
    conn.commit()


# ---------------------------------------------------------------------------
# Inbox processing
# ---------------------------------------------------------------------------

def process_inbox(domains: Optional[dict[str, list[int]]] = None):
    """Process files in the prism/prism-inbox/ folder.

    1. Finds all ingestible files in prism-inbox/ and subdirectories
    2. Assigns each file the next available SXX ID
    3. Moves it to prism-sources/ with clean naming
    4. Ingests the new files (not a full re-ingest)
    5. Auto-classifies domains. Subfolder names are honoured as classification hints
       — files under `prism-inbox/<folder>/...` are assigned to the domain matching that
       folder name (the domain is seeded if it does not yet exist).
    6. Wires source nodes to domain nodes in the graph
    7. Exports graph
    8. Cleans up prism-inbox/
    """
    import shutil

    if not INBOX_DIR.exists():
        print("No prism-inbox/ directory found.")
        return []

    inbox_files = []
    for f in sorted(INBOX_DIR.rglob("*")):
        if f.is_dir():
            continue
        if f.name.startswith("."):
            continue
        if f.suffix.lower() in INGEST_EXTENSIONS:
            inbox_files.append(f)

    if not inbox_files:
        print("Inbox is empty — nothing to process.")
        return []

    print(f"Found {len(inbox_files)} file(s) in prism-inbox/")

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    processed = []
    new_paths = []

    for f in inbox_files:
        sid = next_source_id()
        clean_name = re.sub(r'[^a-zA-Z0-9._-]', '-', f.stem).strip('-').lower()
        clean_name = re.sub(r'-+', '-', clean_name)
        new_name = f"{sid}-{clean_name}{f.suffix.lower()}"
        target = SOURCES_DIR / new_name

        rel = f.relative_to(INBOX_DIR)
        rel_parts = rel.parts
        folder_hint = rel_parts[0] if len(rel_parts) > 1 else None

        shutil.move(str(f), str(target))
        print(f"  {rel} -> {target.name} (assigned {sid})")
        processed.append({
            "source_id": sid, "original": f.name,
            "relative_path": str(rel), "target": target.name, "path": target,
            "folder_hint": folder_hint,
        })
        new_paths.append(target)

    # Ingest only the new files
    print(f"\nIngesting {len(processed)} new source(s)...")
    conn = init_db()
    ingest_new_sources(conn, new_paths)

    # Auto-classify and wire domain edges
    print("\n=== Domain Classification ===")
    for item in processed:
        sid = item["source_id"]
        original = item["original"]
        folder_hint = item.get("folder_hint")

        if domains and original in domains:
            domain_nums = domains[original]
            print(f"  {sid}: manual -> domains {domain_nums}")
        elif folder_hint:
            seeded = seed_domain_from_folder(folder_hint)
            if seeded:
                domain_nums = [seeded["id"]]
                print(f"  {sid}: folder hint '{folder_hint}' -> domain {seeded['id']} ({seeded['label']})")
            else:
                domain_nums = []
        else:
            try:
                classified = classify_source_domains(sid, conn)
                domain_nums = [c["domain_id"] for c in classified]
                scores = {c["domain_id"]: c["score"] for c in classified}
                print(f"  {sid}: auto-classified -> domains {domain_nums} (scores: {scores})")
            except Exception as e:
                domain_nums = []
                print(f"  {sid}: classification failed ({e}), unclassified")

        if domain_nums:
            actions = wire_source_to_domains(sid, domain_nums, conn)
            for a in actions:
                print(f"    {a}")
            item["domains"] = domain_nums

    # Stats
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_embeds = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    print(f"\nDone. Chunks: {n_chunks} | Embeddings: {n_embeds}")

    _save_stats_snapshot(conn)
    conn.close()
    export_db_to_workspace()

    # Export graph
    export_graph_json()

    # Clean up inbox
    for d in sorted(INBOX_DIR.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass
    for f in INBOX_DIR.rglob("*"):
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass
    for d in sorted(INBOX_DIR.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass

    print(f"Inbox cleaned.")
    return processed


# ---------------------------------------------------------------------------
# Multi-modal ingestion: URLs and pasted text
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    """Best-effort HTML→text. Tries readability-lxml/bs4 if available; falls back to regex."""
    try:
        from readability import Document  # type: ignore
        from bs4 import BeautifulSoup  # type: ignore
        doc = Document(html)
        soup = BeautifulSoup(doc.summary(), "html.parser")
        return soup.get_text(separator="\n").strip()
    except Exception:
        pass

    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside"]):
            tag.decompose()
        return soup.get_text(separator="\n").strip()
    except Exception:
        pass

    text = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def ingest_url(url: str, title: Optional[str] = None,
               domain_hint: Optional[str] = None) -> dict:
    """Fetch a URL, extract text, and ingest as a source.

    Returns {source_id, title, url, domains}.
    """
    from urllib.request import Request, urlopen

    print(f"Fetching {url}...")
    try:
        req = Request(url, headers={"User-Agent": "PRISM/1.0"})
        with urlopen(req, timeout=30) as resp:
            html_bytes = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except Exception as e:
        raise RuntimeError(f"Could not fetch {url}: {e}")

    encoding = "utf-8"
    if "charset=" in content_type:
        encoding = content_type.split("charset=")[-1].split(";")[0].strip()

    try:
        html = html_bytes.decode(encoding, errors="replace")
    except LookupError:
        html = html_bytes.decode("utf-8", errors="replace")

    text = _strip_html(html) if "html" in content_type.lower() or html.lstrip().startswith("<") else html

    if not text.strip():
        raise RuntimeError(f"No text extracted from {url}")

    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = m.group(1).strip() if m else url

    metadata = {"url": url, "fetched_at": datetime.now().isoformat()}
    return ingest_text(text=text, title=title, metadata=metadata, domain_hint=domain_hint)


def ingest_text(text: str, title: Optional[str] = None,
                metadata: Optional[dict] = None,
                domain_hint: Optional[str] = None) -> dict:
    """Ingest a passage of text as a new source.

    Writes the text to prism/prism-sources/SXX-<slug>.md, ingests, classifies, wires graph.
    Returns {source_id, title, domains}.
    """
    if not text or not text.strip():
        raise ValueError("ingest_text requires non-empty text")

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    sid = next_source_id()
    title_str = title or f"Pasted source {sid}"
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", title_str.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:60] or "pasted"

    target = SOURCES_DIR / f"{sid}-{slug}.md"

    body = f"# {title_str}\n\n"
    if metadata:
        body += "---\n"
        for k, v in metadata.items():
            body += f"{k}: {v}\n"
        body += "---\n\n"
    body += text.strip() + "\n"
    target.write_text(body)
    print(f"  Wrote {target.name} (assigned {sid})")

    conn = init_db()
    ingest_new_sources(conn, [target])

    domain_nums = []
    if domain_hint:
        seeded = seed_domain_from_folder(domain_hint)
        if seeded:
            domain_nums = [seeded["id"]]
            print(f"  {sid}: hint '{domain_hint}' -> domain {seeded['id']} ({seeded['label']})")

    if not domain_nums:
        try:
            classified = classify_source_domains(sid, conn)
            domain_nums = [c["domain_id"] for c in classified]
            scores = {c["domain_id"]: c["score"] for c in classified}
            print(f"  {sid}: auto-classified -> domains {domain_nums} (scores: {scores})")
        except Exception as e:
            print(f"  {sid}: classification failed ({e}), unclassified")

    if domain_nums:
        wire_source_to_domains(sid, domain_nums, conn)

    _save_stats_snapshot(conn)
    conn.close()
    export_db_to_workspace()
    export_graph_json()

    return {"source_id": sid, "title": title_str, "domains": domain_nums,
            "path": str(target.relative_to(WORKSPACE_ROOT))}


# ---------------------------------------------------------------------------
# Source lifecycle: re-ingest and delete
# ---------------------------------------------------------------------------

def reingest_source(source_id: str) -> dict:
    """Re-read the source file in prism-sources/, regenerate chunks/embeddings,
    keep the existing source node and graph wiring intact.
    """
    source_id = source_id.upper()
    conn = init_db()
    row = conn.execute(
        "SELECT label, metadata FROM nodes WHERE id = ? AND type = 'source'",
        (source_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Source {source_id} not found")

    meta = json.loads(row[1]) if row[1] else {}
    rel_path = meta.get("path")
    if not rel_path:
        conn.close()
        raise ValueError(f"Source {source_id} has no recorded file path")

    full_path = WORKSPACE_ROOT / rel_path
    if not full_path.exists():
        conn.close()
        raise FileNotFoundError(f"Source file not found: {full_path}")

    ingest_new_sources(conn, [full_path])
    _save_stats_snapshot(conn)
    conn.close()
    export_db_to_workspace()
    export_graph_json()

    return {"source_id": source_id, "status": "reingested", "path": rel_path}


def delete_source(source_id: str) -> dict:
    """Delete a source from the brain (chunks, embeddings, edges, node).

    Does NOT delete the file in prism-sources/. The user can manually remove the
    file or call reingest_source if they want it back in the brain.
    """
    source_id = source_id.upper()
    conn = init_db()

    chunk_ids = [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE source_id = ?", (source_id,)
    ).fetchall()]

    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        conn.execute(f"DELETE FROM embeddings WHERE chunk_id IN ({placeholders})", chunk_ids)
    conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (source_id, source_id))
    cur = conn.execute("DELETE FROM nodes WHERE id = ? AND type = 'source'", (source_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    export_db_to_workspace()
    export_graph_json()

    return {"source_id": source_id, "deleted": deleted, "chunks_removed": len(chunk_ids)}


# ---------------------------------------------------------------------------
# Source registry (replaces INDEX.md)
# ---------------------------------------------------------------------------

def source_registry() -> list[dict]:
    """Return the source registry as structured data.

    This replaces INDEX.md — the authoritative source list is a database query.
    Returns list of dicts with: id, title, group_id, chunk_count, domains, metadata.
    """
    conn = init_db()
    rows = conn.execute("""
        SELECT n.id, n.label, n.group_id, n.metadata,
               COUNT(c.id) as chunk_count
        FROM nodes n
        LEFT JOIN chunks c ON c.source_id = n.id
        WHERE n.type = 'source'
        GROUP BY n.id
        ORDER BY n.id
    """).fetchall()

    registry = []
    for r in rows:
        meta = json.loads(r[3]) if r[3] else {}
        domain_rows = conn.execute("""
            SELECT e.source_id, n.label
            FROM edges e
            JOIN nodes n ON n.id = e.source_id
            WHERE e.target_id = ? AND e.type = 'contains'
        """, (r[0],)).fetchall()

        registry.append({
            "id": r[0],
            "title": r[1],
            "group_id": r[2],
            "chunk_count": r[4],
            "domains": [{"id": d[0], "label": d[1]} for d in domain_rows],
            "metadata": meta
        })

    conn.close()
    return registry


# ---------------------------------------------------------------------------
# Graph explorer update
# ---------------------------------------------------------------------------

def update_graph_html():
    """Embed current graph data from prism-brain.db into prism-graph-explorer.html.

    Dynamically regenerates colorMap and legend from domain configuration.
    If prism-graph-explorer.html doesn't exist, tries to copy from the template.
    """
    if not GRAPH_HTML.exists():
        template = ENGINE_DIR.parent / "templates" / "graph-explorer-template.html"
        if template.exists():
            import shutil
            GRAPH_HTML.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(template), str(GRAPH_HTML))
            print(f"  Created prism-graph-explorer.html from template")
        else:
            print("  prism-graph-explorer.html not found (no template available)")
            return False

    data = get_graph_data()
    graph_json = json.dumps(data, indent=2)

    html = GRAPH_HTML.read_text()

    # --- 1. Update graphData ---
    marker_start = "const graphData = "
    start_idx = html.find(marker_start)
    if start_idx == -1:
        print("  Could not find 'const graphData = ' in prism-graph-explorer.html")
        return False

    json_start = start_idx + len(marker_start)
    brace_depth = 0
    end_idx = json_start
    for i in range(json_start, len(html)):
        if html[i] == '{':
            brace_depth += 1
        elif html[i] == '}':
            brace_depth -= 1
            if brace_depth == 0:
                end_idx = i + 1
                break

    if end_idx < len(html) and html[end_idx] == ';':
        end_idx += 1

    html = html[:start_idx] + f"const graphData = {graph_json};" + html[end_idx:]

    # --- 2. Dynamic colorMap from domains config ---
    colors = get_domain_colors()

    all_groups = set()
    for n in data['nodes']:
        if n.get('group') is not None:
            all_groups.add(n['group'])

    color_entries = []
    for g in sorted(all_groups):
        color = colors.get(g, '#666')
        color_entries.append(f"  {g}: '{color}'")

    new_colormap = "const colorMap = {\n" + ",\n".join(color_entries) + "\n};"

    cm_start = html.find("const colorMap = {")
    if cm_start != -1:
        cm_end = html.find("};", cm_start) + 2
        html = html[:cm_start] + new_colormap + html[cm_end:]

    # --- 3. Dynamic legend from domains config ---
    short_labels = get_domain_short_labels()
    domain_nodes = [n for n in data['nodes'] if n['type'] == 'domain']

    sorted_groups = sorted(all_groups)
    legend_items = [
        '  <div class="legend-title">Domains <span id="legend-toggle-all" onclick="toggleAllDomains()">deselect all</span></div>',
        '  <div class="item"><div class="dot" style="background:#e8a735"></div> Domain</div>',
    ]
    for g in sorted_groups:
        color = colors.get(g, '#666')
        label = short_labels.get(g)
        if not label:
            dn = [n for n in domain_nodes if n.get('group') == g]
            label = dn[0]['label'] if dn else f'Group {g}'
        legend_items.append(
            f'  <div class="item" data-group="{g}" onclick="toggleDomain({g})">'
            f'<div class="dot" style="background:{color}"></div> {label}</div>'
        )

    new_legend = '<div id="legend">\n' + '\n'.join(legend_items) + '\n</div>'

    leg_start = html.find('<div id="legend">')
    if leg_start != -1:
        depth = 0
        i = leg_start
        while i < len(html):
            if html[i:i+4] == '<div':
                depth += 1
            elif html[i:i+6] == '</div>':
                depth -= 1
                if depth == 0:
                    leg_end = i + 6
                    break
            i += 1
        html = html[:leg_start] + new_legend + html[leg_end:]

    if 'data-group=' not in html or 'toggleDomain(' not in html or 'legend-toggle-all' not in html:
        print("  WARNING: Legend interactivity attributes missing after regeneration!")

    GRAPH_HTML.write_text(html)

    print(f"  Updated prism-graph-explorer.html: {len(data['nodes'])} nodes, {len(data['edges'])} edges")
    return True


# ---------------------------------------------------------------------------
# Prediction narrative ingestion (51Folds — preserved for Phase 6)
# ---------------------------------------------------------------------------

def ingest_prediction_narrative(model_id: str, narrative_path: Optional[str] = None):
    predictions_dir = PREDICTIONS_DIR / model_id
    if narrative_path:
        npath = Path(narrative_path)
    else:
        npath = predictions_dir / "narrative.md"

    if not npath.exists():
        print(f"Error: Narrative not found at {npath}")
        return

    print(f"=== Ingesting prediction narrative for {model_id} ===")
    conn = init_db()

    pred = get_prediction(model_id)
    source_id = None
    if pred and pred.get("graph_node_id"):
        source_id = pred["graph_node_id"]
    else:
        row = conn.execute(
            "SELECT id FROM nodes WHERE type='prediction' AND metadata LIKE ?",
            (f'%"model_id": "{model_id}"%',)).fetchone()
        if row:
            source_id = row[0]

    if not source_id:
        print(f"  Error: No graph node found for prediction {model_id}.")
        conn.close()
        return

    print(f"  Using graph node {source_id} for narrative chunks")

    text = npath.read_text(errors="replace")
    chunks = chunk_text(text)

    old_ids = [r[0] for r in conn.execute(
        "SELECT id FROM chunks WHERE source_id=?", (source_id,)).fetchall()]
    if old_ids:
        placeholders = ",".join("?" * len(old_ids))
        conn.execute(f"DELETE FROM embeddings WHERE chunk_id IN ({placeholders})", old_ids)
        conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))

    all_chunks = []
    chunk_records = []
    for i, chunk in enumerate(chunks):
        content_hash = hashlib.sha256(chunk["content"][:500].encode()).hexdigest()
        conn.execute(
            "INSERT INTO chunks (source_id, chunk_index, content, content_hash, "
            "char_start, char_end) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, i, chunk["content"], content_hash,
             chunk["char_start"], chunk["char_end"])
        )
        chunk_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        all_chunks.append(chunk["content"])
        chunk_records.append(chunk_id)

    conn.commit()

    if all_chunks:
        print(f"  Generating embeddings for {len(all_chunks)} narrative chunks...")
        embedder = get_embedder()

        all_content = [r[0] for r in conn.execute("SELECT content FROM chunks").fetchall()]
        embedder.fit(all_content)

        vecs = embedder.embed_texts(all_chunks)
        for cid, vec in zip(chunk_records, vecs):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (chunk_id, embedding) VALUES (?, ?)",
                (cid, vec_to_bytes(vec))
            )
        conn.commit()

    print(f"  Ingested {len(all_chunks)} chunks from narrative for model {model_id}")
    conn.close()
    export_db_to_workspace()


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_keyword(conn: sqlite3.Connection, query: str,
                   top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """BM25 keyword search via FTS5."""
    rows = conn.execute("""
        SELECT c.id, c.source_id, c.content, c.chunk_index,
               bm25(chunks_fts) AS score
        FROM chunks_fts f
        JOIN chunks c ON c.id = f.rowid
        WHERE chunks_fts MATCH ?
        ORDER BY score
        LIMIT ?
    """, (query, top_k)).fetchall()

    return [{"chunk_id": r[0], "source_id": r[1], "content": r[2],
             "chunk_index": r[3], "score": -r[4], "method": "keyword"}
            for r in rows]


def search_semantic(conn: sqlite3.Connection, query: str,
                    top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """Vector cosine similarity search."""
    embedder = get_embedder()

    if isinstance(embedder, TFIDFEmbedder) and not embedder._fitted:
        all_content = [r[0] for r in conn.execute("SELECT content FROM chunks").fetchall()]
        embedder.fit(all_content)

    query_vec = embedder.embed_single(query)

    rows = conn.execute("""
        SELECT c.id, c.source_id, c.content, c.chunk_index, e.embedding
        FROM chunks c
        JOIN embeddings e ON e.chunk_id = c.id
    """).fetchall()

    scored = []
    for r in rows:
        chunk_vec = bytes_to_vec(r[4])
        sim = cosine_similarity(query_vec, chunk_vec)
        scored.append({
            "chunk_id": r[0], "source_id": r[1], "content": r[2],
            "chunk_index": r[3], "score": sim, "method": "semantic"
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def search_hybrid(conn: sqlite3.Connection, query: str,
                  top_k: int = DEFAULT_TOP_K,
                  keyword_weight: float = 1.0,
                  semantic_weight: float = 1.0) -> list[dict]:
    """Hybrid search using Reciprocal Rank Fusion."""
    kw_results = search_keyword(conn, query, top_k=top_k * 2)
    sem_results = search_semantic(conn, query, top_k=top_k * 2)

    rrf_scores = {}
    chunk_data = {}

    for rank, r in enumerate(kw_results):
        cid = r["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0) + keyword_weight / (RRF_K + rank + 1)
        chunk_data[cid] = r

    for rank, r in enumerate(sem_results):
        cid = r["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0) + semantic_weight / (RRF_K + rank + 1)
        if cid not in chunk_data:
            chunk_data[cid] = r

    results = []
    for cid, score in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True):
        entry = chunk_data[cid].copy()
        entry["rrf_score"] = score
        entry["method"] = "hybrid"
        results.append(entry)

    return results[:top_k]


def search(query: str, mode: str = "hybrid", top_k: int = DEFAULT_TOP_K) -> list[dict]:
    """Main search entry point."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        if mode == "keyword":
            results = search_keyword(conn, query, top_k)
        elif mode == "semantic":
            results = search_semantic(conn, query, top_k)
        else:
            results = search_hybrid(conn, query, top_k)
    finally:
        conn.close()

    return results


def format_results(results: list[dict], verbose: bool = False) -> str:
    """Format search results for display."""
    lines = []
    for i, r in enumerate(results):
        score_key = "rrf_score" if "rrf_score" in r else "score"
        score = r[score_key]
        source = r["source_id"]
        method = r["method"]

        lines.append(f"\n--- Result {i+1} [{method}] score={score:.4f} source={source} ---")
        content = r["content"]
        if not verbose and len(content) > 600:
            content = content[:600] + "..."
        lines.append(content)

    return "\n".join(lines)


def format_context(results: list[dict], max_chars: int = 8000) -> str:
    """Format results as LLM context block."""
    lines = [f"<context source=\"prism\" chunks=\"{len(results)}\">"]
    char_count = 0
    for r in results:
        chunk_text_str = f"\n<chunk source=\"{r['source_id']}\" index=\"{r['chunk_index']}\">\n{r['content']}\n</chunk>"
        if char_count + len(chunk_text_str) > max_chars:
            break
        lines.append(chunk_text_str)
        char_count += len(chunk_text_str)
    lines.append("\n</context>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Graph queries
# ---------------------------------------------------------------------------

def graph_neighbors(node_id: str, hops: int = 1) -> dict:
    """Get all nodes within N hops of a given node."""
    conn = sqlite3.connect(str(DB_PATH))

    visited = set()
    frontier = {node_id}
    all_edges = []

    for hop in range(hops):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = conn.execute(f"""
            SELECT source_id, target_id, type, label
            FROM edges
            WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})
        """, list(frontier) + list(frontier)).fetchall()

        visited.update(frontier)
        new_frontier = set()
        for r in rows:
            all_edges.append({"source": r[0], "target": r[1],
                             "type": r[2], "label": r[3]})
            if r[0] not in visited:
                new_frontier.add(r[0])
            if r[1] not in visited:
                new_frontier.add(r[1])
        frontier = new_frontier

    visited.update(frontier)

    if visited:
        placeholders = ",".join("?" * len(visited))
        nodes = conn.execute(
            f"SELECT id, label, type, group_id FROM nodes WHERE id IN ({placeholders})",
            list(visited)
        ).fetchall()
    else:
        nodes = []

    conn.close()

    return {
        "center": node_id,
        "hops": hops,
        "nodes": [{"id": n[0], "label": n[1], "type": n[2], "group": n[3]}
                  for n in nodes],
        "edges": all_edges
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_graph_json():
    """Export graph from prism-brain.db to prism-graph.json and update prism-graph-explorer.html."""
    data = get_graph_data()

    GRAPH_JSON.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_JSON.write_text(json.dumps(data, indent=2))
    print(f"Exported to {GRAPH_JSON}: {len(data['nodes'])} nodes, {len(data['edges'])} edges")

    update_graph_html()
    print("Graph export complete.")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats_dict() -> dict:
    """Return database statistics as a structured dict."""
    conn = init_db()

    result = {
        "sources": conn.execute("SELECT COUNT(*) FROM nodes WHERE type='source'").fetchone()[0],
        "concepts": conn.execute("SELECT COUNT(*) FROM nodes WHERE type='concept'").fetchone()[0],
        "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "embeddings": conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "domains": conn.execute("SELECT COUNT(*) FROM nodes WHERE type='domain'").fetchone()[0],
        "db_size_kb": round(DB_PATH.stat().st_size / 1024) if DB_PATH.exists() else 0,
    }

    cross_domain = conn.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN nodes n1 ON n1.id = e.source_id
        JOIN nodes n2 ON n2.id = e.target_id
        WHERE n1.group_id IS NOT NULL AND n2.group_id IS NOT NULL
          AND n1.group_id != n2.group_id
    """).fetchone()[0]
    result["cross_domain_edges"] = cross_domain

    last_ingest = conn.execute("SELECT value FROM meta WHERE key='last_ingest'").fetchone()
    result["last_ingest"] = last_ingest[0] if last_ingest else None

    node_types = {}
    for row in conn.execute("SELECT type, COUNT(*) FROM nodes GROUP BY type"):
        node_types[row[0]] = row[1]
    result["node_types"] = node_types

    edge_types = {}
    for row in conn.execute("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY COUNT(*) DESC"):
        edge_types[row[0]] = row[1]
    result["edge_types"] = edge_types

    conn.close()
    return result


def stats():
    """Print database statistics."""
    conn = sqlite3.connect(str(DB_PATH))

    n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_embeds = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    db_size = DB_PATH.stat().st_size / 1024

    last_ingest = conn.execute(
        "SELECT value FROM meta WHERE key='last_ingest'").fetchone()

    print(f"=== PRISM — Brain Stats ===")
    print(f"  Database: {DB_PATH}")
    print(f"  Size: {db_size:.0f} KB")
    print(f"  Nodes: {n_nodes}")
    print(f"  Edges: {n_edges}")
    print(f"  Chunks: {n_chunks}")
    print(f"  Embeddings: {n_embeds}")
    if last_ingest:
        print(f"  Last ingest: {last_ingest[0]}")

    for row in conn.execute("SELECT type, COUNT(*) FROM nodes GROUP BY type"):
        print(f"    {row[0]}: {row[1]}")

    print(f"  Edge types:")
    for row in conn.execute("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY COUNT(*) DESC"):
        print(f"    {row[0]}: {row[1]}")

    cross_domain = conn.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN nodes n1 ON n1.id = e.source_id
        JOIN nodes n2 ON n2.id = e.target_id
        WHERE n1.group_id IS NOT NULL AND n2.group_id IS NOT NULL
          AND n1.group_id != n2.group_id
    """).fetchone()[0]
    print(f"  Cross-domain edges: {cross_domain}")

    print(f"  Chunks by source:")
    for row in conn.execute("""
        SELECT n.label, COUNT(c.id)
        FROM chunks c JOIN nodes n ON n.id = c.source_id
        GROUP BY c.source_id ORDER BY COUNT(c.id) DESC
    """):
        print(f"    {row[0]}: {row[1]}")

    conn.close()


# ---------------------------------------------------------------------------
# Full ingestion pipeline
# ---------------------------------------------------------------------------

def ingest():
    """Full ingestion pipeline."""
    print("=== Ingesting PRISM Knowledge Base ===")
    conn = init_db()

    print("1. Importing graph structure...")
    ingest_graph_json(conn)

    print("2. Chunking and embedding documents...")
    ingest_documents(conn)

    # Stats
    n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_embeds = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    print(f"\n=== Done ===")
    print(f"  Nodes: {n_nodes}  |  Edges: {n_edges}")
    print(f"  Chunks: {n_chunks}  |  Embeddings: {n_embeds}")
    print(f"  Database: {DB_PATH} ({DB_PATH.stat().st_size / 1024:.0f} KB)")

    _save_stats_snapshot(conn)
    conn.close()

    export_db_to_workspace()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "ingest":
        ingest()

    elif cmd == "search":
        mode = "hybrid"
        top_k = DEFAULT_TOP_K
        query_parts = []
        context_mode = False
        verbose = False

        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "--mode" and i + 1 < len(sys.argv):
                mode = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--top" and i + 1 < len(sys.argv):
                top_k = int(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--context":
                context_mode = True
                i += 1
            elif sys.argv[i] == "--verbose":
                verbose = True
                i += 1
            else:
                query_parts.append(sys.argv[i])
                i += 1

        query = " ".join(query_parts)
        if not query:
            print("Error: no query provided")
            return

        results = search(query, mode=mode, top_k=top_k)

        if context_mode:
            print(format_context(results))
        else:
            print(f"=== {mode.upper()} search: \"{query}\" ({len(results)} results) ===")
            print(format_results(results, verbose=verbose))

    elif cmd == "graph":
        if len(sys.argv) < 3:
            print("Usage: brain.py graph <node_id> [--hops N]")
            return
        node_id = sys.argv[2]
        hops = 1
        if "--hops" in sys.argv:
            idx = sys.argv.index("--hops")
            hops = int(sys.argv[idx + 1])
        result = graph_neighbors(node_id, hops)
        print(json.dumps(result, indent=2))

    elif cmd == "export-graph":
        export_graph_json()

    elif cmd == "ingest-prediction":
        if len(sys.argv) < 3:
            print("Usage: brain.py ingest-prediction <model_id> [narrative_path]")
            return
        model_id = sys.argv[2]
        narrative_path = sys.argv[3] if len(sys.argv) > 3 else None
        ingest_prediction_narrative(model_id, narrative_path)

    elif cmd == "inbox":
        process_inbox()

    elif cmd == "tag":
        if len(sys.argv) < 4:
            print("Usage: brain.py tag <source_id> <domain1,domain2,...>")
            return
        source_id = sys.argv[2].upper()
        domain_nums = [int(d.strip()) for d in sys.argv[3].split(",")]
        tag_source(source_id, domain_nums)

    elif cmd == "registry":
        registry = source_registry()
        print(json.dumps(registry, indent=2))

    elif cmd == "stats":
        stats()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
