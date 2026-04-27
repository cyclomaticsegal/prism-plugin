"""Engine unit tests: ingest, search, graph, tag, stats, export, dedup, rollback."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Set env before importing brain so it doesn't try to restore from a nonexistent workspace
os.environ["PRISM_AUTO_RESTORE"] = "0"

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
import brain


FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_SOURCES = FIXTURES / "sample-sources"
DOMAINS_TEST = FIXTURES / "domains-test.json"


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    """Create a fresh workspace for each test (using the prism/ container)."""
    # Configure engine to use this workspace — sets brain.SOURCES_DIR etc.
    brain.configure(workspace_root=str(tmp_path), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()

    brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(SAMPLE_SOURCES.glob("*.md")):
        shutil.copy2(str(f), str(brain.SOURCES_DIR / f.name))

    # Force TF-IDF backend for test speed
    brain.EMBEDDING_BACKEND = "tfidf"

    yield tmp_path

    # Clean up
    brain.EMBEDDING_BACKEND = "auto"


@pytest.fixture
def db(workspace):
    """Return a connection to a freshly initialized DB."""
    conn = brain.init_db()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class TestConfigure:
    def test_configure_sets_workspace_root(self, tmp_path):
        brain.configure(workspace_root=str(tmp_path))
        assert brain.WORKSPACE_ROOT == tmp_path

    def test_configure_updates_db_path(self, tmp_path):
        brain.configure(workspace_root=str(tmp_path))
        assert brain.DB_PATH == tmp_path / "prism" / "prism-brain.db"

    def test_configure_updates_graph_paths(self, tmp_path):
        brain.configure(workspace_root=str(tmp_path))
        assert brain.GRAPH_JSON == tmp_path / "prism" / "prism-graph.json"
        assert brain.GRAPH_HTML == tmp_path / "prism" / "prism-graph-explorer.html"

    def test_configure_session_dir(self, tmp_path):
        session = tmp_path / "session"
        session.mkdir()
        brain.configure(workspace_root=str(tmp_path), session_dir=str(session))
        assert brain.DB_PATH == session / "prism-brain.db"
        assert brain.DB_EXPORT_PATH == tmp_path / "prism" / "prism-brain.db"

    def test_configure_resets_domains_cache(self, tmp_path):
        brain.load_domains()
        brain.configure(workspace_root=str(tmp_path))
        brain.reset_domains_cache()
        # Should not raise
        brain.load_domains()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class TestDatabase:
    def test_init_db_creates_tables(self, db):
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        assert "nodes" in tables
        assert "edges" in tables
        assert "chunks" in tables
        assert "embeddings" in tables
        assert "meta" in tables

    def test_init_db_creates_fts(self, db):
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        assert "chunks_fts" in tables

    def test_init_db_idempotent(self, workspace):
        conn1 = brain.init_db()
        conn1.close()
        conn2 = brain.init_db()
        tables = [r[0] for r in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn2.close()
        assert len(tables) > 0


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

class TestChunking:
    def test_chunk_text_basic(self):
        text = "Hello world. " * 100
        chunks = brain.chunk_text(text)
        assert len(chunks) >= 1
        assert all("content" in c for c in chunks)
        assert all("char_start" in c for c in chunks)

    def test_chunk_text_small_input(self):
        chunks = brain.chunk_text("Short text.")
        assert len(chunks) == 1
        assert chunks[0]["content"] == "Short text."

    def test_chunk_text_respects_sections(self):
        text = "Section one content.\n\n---\n\nSection two content."
        chunks = brain.chunk_text(text)
        assert len(chunks) >= 1

    def test_chunk_text_overlap(self):
        long_text = ("This is a paragraph about topic A. " * 200 + "\n\n" +
                     "This is a paragraph about topic B. " * 200)
        chunks = brain.chunk_text(long_text, chunk_chars=500, overlap_chars=100)
        assert len(chunks) > 1


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

class TestIngestion:
    def test_find_source_files(self, workspace):
        files = brain.find_source_files()
        assert len(files) == 5
        assert all(f.suffix == ".md" for f in files)

    def test_file_to_source_id_with_prefix(self, workspace):
        path = brain.SOURCES_DIR / "S01-monetary-policy.md"
        assert brain.file_to_source_id(path) == "S01"

    def test_file_to_source_id_without_prefix(self, workspace):
        path = brain.SOURCES_DIR / "random-file.md"
        sid = brain.file_to_source_id(path)
        assert sid.startswith("S_")

    def test_ingest_documents(self, db, workspace):
        brain.ingest_documents(db)

        n_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_embeds = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        n_nodes = db.execute("SELECT COUNT(*) FROM nodes WHERE type='source'").fetchone()[0]

        assert n_chunks > 0
        assert n_embeds == n_chunks
        assert n_nodes == 5

    def test_ingest_creates_source_nodes(self, db, workspace):
        brain.ingest_documents(db)

        nodes = db.execute(
            "SELECT id FROM nodes WHERE type='source' ORDER BY id"
        ).fetchall()
        ids = [n[0] for n in nodes]
        assert "S01" in ids
        assert "S02" in ids

    def test_ingest_updates_meta(self, db, workspace):
        brain.ingest_documents(db)

        last_ingest = db.execute(
            "SELECT value FROM meta WHERE key='last_ingest'"
        ).fetchone()
        assert last_ingest is not None

    def test_ingest_idempotent(self, db, workspace):
        brain.ingest_documents(db)
        count1 = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        brain.reset_embedder()
        brain.ingest_documents(db)
        count2 = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        assert count2 == count1


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

class TestDuplicateDetection:
    def test_duplicate_source_re_ingested_cleanly(self, db, workspace):
        brain.ingest_documents(db)
        chunks_before = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        brain.reset_embedder()
        brain.ingest_documents(db)
        chunks_after = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

        assert chunks_after == chunks_before

    def test_content_hash_stored(self, db, workspace):
        brain.ingest_documents(db)

        hashes = db.execute("SELECT content_hash FROM chunks LIMIT 5").fetchall()
        assert all(len(h[0]) == 64 for h in hashes)  # SHA-256 hex


# ---------------------------------------------------------------------------
# Transaction rollback
# ---------------------------------------------------------------------------

class TestTransactionRollback:
    def test_failed_source_rolls_back(self, db, workspace):
        brain.ingest_documents(db)
        initial_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        initial_nodes = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

        bad_source = brain.SOURCES_DIR / "S99-bad-source.md"
        bad_source.write_text("Some content here about a topic.")

        original_ingest = brain._ingest_single_source

        def failing_ingest(conn, path, source_id, chunks, embedder):
            if source_id == "S99":
                raise ValueError("Simulated ingestion failure")
            return original_ingest(conn, path, source_id, chunks, embedder)

        brain._ingest_single_source = failing_ingest
        brain.reset_embedder()

        try:
            brain.ingest_documents(db)
        finally:
            brain._ingest_single_source = original_ingest

        final_chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert final_chunks == initial_chunks

        final_nodes = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert final_nodes == initial_nodes


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestSearch:
    @pytest.fixture(autouse=True)
    def _ingest(self, db, workspace):
        brain.ingest_documents(db)
        db.commit()

    def test_keyword_search(self, db):
        results = brain.search_keyword(db, "monetary policy", top_k=3)
        assert len(results) > 0
        assert results[0]["method"] == "keyword"
        assert "source_id" in results[0]

    def test_semantic_search(self, db):
        results = brain.search_semantic(db, "central banking", top_k=3)
        assert len(results) > 0
        assert results[0]["method"] == "semantic"

    def test_hybrid_search(self, db):
        results = brain.search_hybrid(db, "interest rates inflation", top_k=3)
        assert len(results) > 0
        assert results[0]["method"] == "hybrid"
        assert "rrf_score" in results[0]

    def test_search_entry_point(self, workspace):
        results = brain.search("artificial intelligence", top_k=3)
        assert len(results) > 0

    def test_search_returns_relevant_source(self, workspace):
        results = brain.search("transformer neural network LLM", top_k=3)
        source_ids = [r["source_id"] for r in results]
        assert "S02" in source_ids

    def test_default_top_k_is_3(self):
        assert brain.DEFAULT_TOP_K == 3

    def test_format_results(self, workspace):
        results = brain.search("energy", top_k=2)
        formatted = brain.format_results(results)
        assert "Result 1" in formatted

    def test_format_context(self, workspace):
        results = brain.search("energy", top_k=2)
        ctx = brain.format_context(results)
        assert '<context source="prism"' in ctx


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

class TestGraph:
    def test_add_node(self, db):
        inserted = brain.add_node("C01", "Test Concept", "concept", group_id=1, conn=db)
        assert inserted is True

        row = db.execute("SELECT label, type, group_id FROM nodes WHERE id='C01'").fetchone()
        assert row == ("Test Concept", "concept", 1)

    def test_add_node_update(self, db):
        brain.add_node("C01", "Original", "concept", conn=db)
        inserted = brain.add_node("C01", "Updated", "concept", conn=db)
        assert inserted is False

        row = db.execute("SELECT label FROM nodes WHERE id='C01'").fetchone()
        assert row[0] == "Updated"

    def test_add_edge(self, db):
        brain.add_node("C01", "A", "concept", conn=db)
        brain.add_node("C02", "B", "concept", conn=db)

        inserted = brain.add_edge("C01", "C02", "relates_to", conn=db)
        assert inserted is True

    def test_add_edge_duplicate(self, db):
        brain.add_node("C01", "A", "concept", conn=db)
        brain.add_node("C02", "B", "concept", conn=db)
        brain.add_edge("C01", "C02", "relates_to", conn=db)

        inserted = brain.add_edge("C01", "C02", "relates_to", conn=db)
        assert inserted is False

    def test_graph_neighbors(self, db, workspace):
        brain.add_node("D1", "Domain 1", "domain", group_id=1, conn=db)
        brain.add_node("C01", "Concept A", "concept", group_id=1, conn=db)
        brain.add_node("C02", "Concept B", "concept", group_id=1, conn=db)
        brain.add_edge("D1", "C01", "contains", conn=db)
        brain.add_edge("C01", "C02", "relates_to", conn=db)

        result = brain.graph_neighbors("C01", hops=1)
        assert result["center"] == "C01"
        node_ids = {n["id"] for n in result["nodes"]}
        assert "D1" in node_ids
        assert "C02" in node_ids

    def test_graph_neighbors_2_hops(self, db, workspace):
        brain.add_node("D1", "Domain 1", "domain", group_id=1, conn=db)
        brain.add_node("C01", "A", "concept", group_id=1, conn=db)
        brain.add_node("C02", "B", "concept", group_id=1, conn=db)
        brain.add_node("C03", "C", "concept", group_id=1, conn=db)
        brain.add_edge("D1", "C01", "contains", conn=db)
        brain.add_edge("C01", "C02", "relates_to", conn=db)
        brain.add_edge("C02", "C03", "relates_to", conn=db)

        result = brain.graph_neighbors("C01", hops=2)
        node_ids = {n["id"] for n in result["nodes"]}
        assert "C03" in node_ids

    def test_get_graph_data(self, db, workspace):
        brain.add_node("D1", "Domain 1", "domain", group_id=1, conn=db)
        brain.add_node("C01", "Concept", "concept", group_id=1, conn=db)
        brain.add_edge("D1", "C01", "contains", conn=db)

        data = brain.get_graph_data()
        assert data["meta"]["title"] == "PRISM — Knowledge Graph"
        node_ids = {n["id"] for n in data["nodes"]}
        assert "D1" in node_ids
        assert "C01" in node_ids
        assert len(data["edges"]) >= 1


# ---------------------------------------------------------------------------
# Tag
# ---------------------------------------------------------------------------

class TestTag:
    def test_tag_source(self, db, workspace):
        brain.ingest_documents(db)

        brain.tag_source("S01", [1, 2])

        # Check edges
        conn = brain.init_db()
        edges = conn.execute(
            "SELECT source_id FROM edges WHERE target_id='S01' AND type='contains'"
        ).fetchall()
        domain_ids = [e[0] for e in edges]
        assert "D1" in domain_ids
        conn.close()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_snapshot(self, db, workspace):
        brain.ingest_documents(db)
        brain._save_stats_snapshot(db)

        row = db.execute("SELECT value FROM meta WHERE key='stats_history'").fetchone()
        assert row is not None
        history = json.loads(row[0])
        assert len(history) == 1
        assert "sources" in history[0]
        assert "chunks" in history[0]
        assert "cross_domain_edges" in history[0]

    def test_stats_snapshot_appends(self, db, workspace):
        brain.ingest_documents(db)
        brain._save_stats_snapshot(db)
        brain._save_stats_snapshot(db)

        row = db.execute("SELECT value FROM meta WHERE key='stats_history'").fetchone()
        history = json.loads(row[0])
        assert len(history) >= 2


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

class TestSourceRegistry:
    def test_source_registry(self, db, workspace):
        brain.ingest_documents(db)
        db.commit()

        registry = brain.source_registry()
        assert len(registry) == 5
        assert all("id" in r for r in registry)
        assert all("title" in r for r in registry)
        assert all("chunk_count" in r for r in registry)

    def test_source_registry_with_domains(self, db, workspace):
        brain.ingest_documents(db)
        brain.wire_source_to_domains("S01", [1], db)
        db.commit()

        registry = brain.source_registry()
        s01 = [r for r in registry if r["id"] == "S01"][0]
        assert len(s01["domains"]) >= 1
        domain_ids = [d["id"] for d in s01["domains"]]
        assert "D1" in domain_ids


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExport:
    def test_export_graph_json(self, db, workspace):
        brain.add_node("D1", "Domain 1", "domain", group_id=1, conn=db)
        brain.add_node("C01", "Test", "concept", group_id=1, conn=db)
        brain.add_edge("D1", "C01", "contains", conn=db)
        db.commit()

        brain.export_graph_json()

        assert brain.GRAPH_JSON.exists()
        data = json.loads(brain.GRAPH_JSON.read_text())
        node_ids = {n["id"] for n in data["nodes"]}
        assert "D1" in node_ids
        assert "C01" in node_ids
        assert len(data["edges"]) >= 1


# ---------------------------------------------------------------------------
# Inbox processing
# ---------------------------------------------------------------------------

class TestInbox:
    def test_process_inbox(self, workspace):
        inbox = brain.INBOX_DIR
        inbox.mkdir(parents=True, exist_ok=True)

        new_source = inbox / "new-article.md"
        new_source.write_text("# New Article\n\nThis is about monetary policy and interest rates.\n")

        processed = brain.process_inbox()
        assert len(processed) == 1
        assert processed[0]["source_id"].startswith("S")

        # File should have moved to sources/
        assert not new_source.exists()
        sources = list(brain.SOURCES_DIR.glob("*.md"))
        assert len(sources) == 6  # 5 fixtures + 1 new

    def test_process_empty_inbox(self, workspace):
        inbox = brain.INBOX_DIR
        inbox.mkdir(parents=True, exist_ok=True)

        processed = brain.process_inbox()
        assert processed == []

    def test_process_inbox_no_dir(self, workspace):
        processed = brain.process_inbox()
        assert processed == []

    def test_ingest_new_sources(self, db, workspace):
        brain.ingest_documents(db)

        new_path = brain.SOURCES_DIR / "S20-quantum-computing.md"
        new_path.write_text(
            "# Quantum Computing\n\n"
            "Quantum computing leverages quantum mechanical phenomena such as "
            "superposition and entanglement to perform calculations. Unlike classical "
            "bits, quantum bits (qubits) can exist in multiple states simultaneously.\n"
        )

        brain.reset_embedder()
        results = brain.ingest_new_sources(db, [new_path])
        assert len(results) == 1
        assert results[0]["chunk_count"] > 0
        assert results[0]["source_id"] == "S20"

        s20 = db.execute("SELECT id FROM nodes WHERE id='S20'").fetchone()
        assert s20 is not None

        chunks = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_id='S20'"
        ).fetchone()[0]
        assert chunks > 0


# ---------------------------------------------------------------------------
# LLM concept extraction hook
# ---------------------------------------------------------------------------

class TestConceptHook:
    def test_hook_called_during_ingestion(self, db, workspace):
        hook_calls = []

        def mock_hook(source_id, chunk_texts, conn):
            hook_calls.append({"source_id": source_id, "n_chunks": len(chunk_texts)})

        brain.set_concept_extraction_hook(mock_hook)
        try:
            brain.ingest_documents(db)
            assert len(hook_calls) == 5  # One call per source
            assert all(h["n_chunks"] > 0 for h in hook_calls)
        finally:
            brain.set_concept_extraction_hook(None)

    def test_no_hook_by_default(self, db, workspace):
        brain.set_concept_extraction_hook(None)
        brain.ingest_documents(db)
        # Should complete without error
        n = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert n > 0
