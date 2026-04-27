"""Server integration tests: bridge commands, error handling, auto-regeneration."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
import brain
import bridge


FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_SOURCES = FIXTURES / "sample-sources"
DOMAINS_TEST = FIXTURES / "domains-test.json"
BRIDGE_PY = Path(__file__).parent.parent / "engine" / "bridge.py"


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    """Create a fresh workspace with ingested sources."""
    brain.configure(workspace_root=str(tmp_path), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()
    brain.EMBEDDING_BACKEND = "tfidf"

    brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(SAMPLE_SOURCES.glob("*.md")):
        shutil.copy2(str(f), str(brain.SOURCES_DIR / f.name))

    # Ingest so the brain has data to query
    conn = brain.init_db()
    brain.ingest_documents(conn)
    conn.close()

    yield tmp_path

    brain.EMBEDDING_BACKEND = "auto"


# ---------------------------------------------------------------------------
# Bridge: direct Python calls
# ---------------------------------------------------------------------------

class TestBridgeSearch:
    def test_search_returns_results(self, workspace):
        result = bridge.handle({"command": "search", "args": {"query": "monetary policy"}})
        assert isinstance(result, list)
        assert len(result) > 0

    def test_search_result_structure(self, workspace):
        result = bridge.handle({"command": "search", "args": {"query": "AI"}})
        r = result[0]
        assert "chunk_id" in r
        assert "source_id" in r
        assert "source_title" in r
        assert "score" in r
        assert "excerpt" in r
        assert "method" in r

    def test_search_respects_top_k(self, workspace):
        result = bridge.handle({"command": "search", "args": {"query": "energy", "top_k": 1}})
        assert len(result) == 1

    def test_search_enriches_source_title(self, workspace):
        result = bridge.handle({"command": "search", "args": {"query": "central banking interest rates"}})
        titles = [r["source_title"] for r in result]
        assert any("monetary" in t.lower() for t in titles)


class TestBridgeStats:
    def test_stats_returns_dict(self, workspace):
        result = bridge.handle({"command": "stats", "args": {}})
        assert isinstance(result, dict)

    def test_stats_structure(self, workspace):
        result = bridge.handle({"command": "stats", "args": {}})
        assert "sources" in result
        assert "chunks" in result
        assert "nodes" in result
        assert "edges" in result
        assert "embeddings" in result
        assert "domains" in result
        assert "cross_domain_edges" in result

    def test_stats_counts(self, workspace):
        result = bridge.handle({"command": "stats", "args": {}})
        assert result["sources"] >= 5
        assert result["chunks"] > 0
        assert result["embeddings"] > 0


class TestBridgeGraph:
    def test_graph_query(self, workspace):
        # First wire a source to a domain so there's a graph to query
        conn = brain.init_db()
        brain.wire_source_to_domains("S01", [1], conn)
        conn.close()

        result = bridge.handle({"command": "graph", "args": {"node_id": "S01", "hops": 1}})
        assert result["center"] == "S01"
        assert len(result["nodes"]) > 0

    def test_graph_with_hops(self, workspace):
        conn = brain.init_db()
        brain.wire_source_to_domains("S01", [1], conn)
        brain.wire_source_to_domains("S02", [1], conn)
        conn.close()

        result = bridge.handle({"command": "graph", "args": {"node_id": "S01", "hops": 2}})
        node_ids = {n["id"] for n in result["nodes"]}
        assert "D1" in node_ids


class TestBridgeIndex:
    def test_index_returns_registry(self, workspace):
        result = bridge.handle({"command": "index", "args": {}})
        assert isinstance(result, list)
        assert len(result) >= 5

    def test_index_entry_structure(self, workspace):
        result = bridge.handle({"command": "index", "args": {}})
        entry = result[0]
        assert "id" in entry
        assert "title" in entry
        assert "chunk_count" in entry
        assert "domains" in entry


class TestBridgeExport:
    def test_export_creates_files(self, workspace):
        result = bridge.handle({"command": "export", "args": {}})
        assert result["status"] == "ok"
        assert (brain.GRAPH_JSON).exists()


class TestBridgeTag:
    def test_tag_source(self, workspace):
        result = bridge.handle({"command": "tag", "args": {"source_id": "S01", "domains": [1, 2]}})
        assert result["status"] == "ok"
        assert result["source_id"] == "S01"
        assert result["domains"] == [1, 2]

    def test_tag_triggers_export(self, workspace):
        bridge.handle({"command": "tag", "args": {"source_id": "S01", "domains": [1]}})
        assert (brain.GRAPH_JSON).exists()


class TestBridgeIngest:
    def test_ingest_empty_inbox(self, workspace):
        result = bridge.handle({"command": "ingest", "args": {}})
        assert result == []

    def test_ingest_processes_inbox(self, workspace):
        inbox = brain.INBOX_DIR
        inbox.mkdir()
        (inbox / "new-source.md").write_text("# Test\n\nNew content about quantum computing.\n")

        brain.reset_embedder()
        result = bridge.handle({"command": "ingest", "args": {}})
        assert len(result) == 1
        assert "source_id" in result[0]


# ---------------------------------------------------------------------------
# Bridge: error handling
# ---------------------------------------------------------------------------

class TestBridgeErrors:
    def test_unknown_command(self, workspace):
        with pytest.raises(ValueError, match="Unknown command"):
            bridge.handle({"command": "nonexistent", "args": {}})

    def test_search_missing_query(self, workspace):
        with pytest.raises((KeyError, TypeError)):
            bridge.handle({"command": "search", "args": {}})


# ---------------------------------------------------------------------------
# Bridge: subprocess mode (as the MCP server would call it)
# ---------------------------------------------------------------------------

class TestBridgeSubprocess:
    def _call_bridge(self, request_dict):
        """Helper: call bridge.py as subprocess, return parsed response."""
        env = {**os.environ, "PRISM_EMBEDDING_BACKEND": "tfidf"}
        result = subprocess.run(
            ["python3", str(BRIDGE_PY)],
            input=json.dumps(request_dict),
            capture_output=True, text=True, timeout=30,
            env=env,
        )
        return json.loads(result.stdout)

    def test_subprocess_search(self, workspace):
        response = self._call_bridge({
            "command": "search",
            "args": {"query": "energy", "top_k": 2},
            "workspace": str(workspace),
        })
        assert response["ok"] is True, f"Bridge error: {response.get('error')}"
        assert len(response["result"]) > 0

    def test_subprocess_stats(self, workspace):
        response = self._call_bridge({
            "command": "stats",
            "args": {},
            "workspace": str(workspace),
        })
        assert response["ok"] is True, f"Bridge error: {response.get('error')}"
        assert response["result"]["sources"] >= 5

    def test_subprocess_empty_input(self):
        result = subprocess.run(
            ["python3", str(BRIDGE_PY)],
            input="", capture_output=True, text=True, timeout=10,
        )
        response = json.loads(result.stdout)
        assert response["ok"] is False

    def test_subprocess_invalid_json(self):
        result = subprocess.run(
            ["python3", str(BRIDGE_PY)],
            input="not json{{{", capture_output=True, text=True, timeout=10,
        )
        response = json.loads(result.stdout)
        assert response["ok"] is False
        assert "Invalid JSON" in response["error"]

    def test_subprocess_unknown_command(self, workspace):
        request = json.dumps({
            "command": "bad_command",
            "args": {},
            "workspace": str(workspace),
        })
        result = subprocess.run(
            ["python3", str(BRIDGE_PY)],
            input=request, capture_output=True, text=True, timeout=10,
        )
        response = json.loads(result.stdout)
        assert response["ok"] is False
        assert "Unknown command" in response["error"]
