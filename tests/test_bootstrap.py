"""Bootstrap tests: first-run creates correct structure, second run is idempotent.

Architecture: prism-brain.db is the single source of truth for domains and
axioms. prism-axioms.md is a regenerated read-only projection of the axioms
table. Everything lives under <workspace>/prism/ with prism- prefixed children.
"""

import json
import os
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
import brain
import bridge


@pytest.fixture
def empty_workspace(tmp_path):
    """An empty workspace — simulates a brand new user."""
    brain.configure(workspace_root=str(tmp_path), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()
    brain.EMBEDDING_BACKEND = "tfidf"
    yield tmp_path
    brain.EMBEDDING_BACKEND = "auto"


class TestFirstRun:
    def test_bootstrap_creates_container(self, empty_workspace):
        bridge.bootstrap_workspace()
        assert (empty_workspace / "prism").is_dir()

    def test_bootstrap_creates_directories(self, empty_workspace):
        bridge.bootstrap_workspace()
        prism = empty_workspace / "prism"
        assert (prism / "prism-inbox").is_dir()
        assert (prism / "prism-sources").is_dir()
        assert (prism / "prism-extensions").is_dir()

    def test_bootstrap_creates_brain_db(self, empty_workspace):
        bridge.bootstrap_workspace()
        db_path = empty_workspace / "prism" / "prism-brain.db"
        assert db_path.exists()
        assert db_path.stat().st_size > 0

    def test_bootstrap_does_not_create_legacy_files(self, empty_workspace):
        """No loose brain.db, AXIOMS.md, GRAPH.json, or domains.json at workspace root."""
        bridge.bootstrap_workspace()
        for legacy in ["brain.db", "AXIOMS.md", "GRAPH.json", "domains.json", "graph-explorer.html"]:
            assert not (empty_workspace / legacy).exists(), f"legacy file {legacy} should not exist at root"

    def test_bootstrap_creates_empty_domains_table(self, empty_workspace):
        """Domains table exists but starts empty (no hardcoded list)."""
        bridge.bootstrap_workspace()
        domains = brain.list_domains_table()
        assert domains == []

    def test_bootstrap_creates_empty_axioms_table(self, empty_workspace):
        """Axioms table exists but starts empty."""
        bridge.bootstrap_workspace()
        axioms = brain.list_axioms()
        assert axioms == []

    def test_bootstrap_writes_axioms_projection(self, empty_workspace):
        """prism-axioms.md is regenerated as a read-only projection on bootstrap."""
        bridge.bootstrap_workspace()
        axioms_path = empty_workspace / "prism" / "prism-axioms.md"
        assert axioms_path.exists()
        content = axioms_path.read_text()
        assert "Generated from" in content
        assert "do not hand-edit" in content.lower() or "Edit through Claude" in content

    def test_bootstrap_reports_new(self, empty_workspace):
        result = bridge.bootstrap_workspace()
        assert result["is_new"] is True
        assert len(result["created"]) > 0
        assert result["workspace"] == str(empty_workspace)

    def test_bootstrap_returns_stats(self, empty_workspace):
        result = bridge.bootstrap_workspace()
        assert "stats" in result
        assert result["stats"]["sources"] == 0
        assert result["stats"]["chunks"] == 0


class TestIdempotency:
    def test_second_run_creates_nothing(self, empty_workspace):
        first = bridge.bootstrap_workspace()
        assert first["is_new"] is True

        brain.reset_domains_cache()
        second = bridge.bootstrap_workspace()
        assert second["is_new"] is False
        assert len(second["created"]) == 0

    def test_preserves_existing_domains(self, empty_workspace):
        """Domain rows added through the API survive subsequent bootstraps."""
        bridge.bootstrap_workspace()
        brain.upsert_domain(label="Custom Domain", keywords="custom test")

        brain.reset_domains_cache()
        bridge.bootstrap_workspace()

        domains = brain.list_domains_table()
        labels = [d["label"] for d in domains]
        assert "Custom Domain" in labels

    def test_preserves_existing_axioms(self, empty_workspace):
        """Axiom rows survive subsequent bootstraps; prism-axioms.md regenerates."""
        bridge.bootstrap_workspace()
        brain.revise_axiom(key="test", body="A test axiom claim.")

        bridge.bootstrap_workspace()

        axioms = brain.list_axioms()
        assert any(a["key"] == "test" for a in axioms)

    def test_preserves_existing_db(self, empty_workspace):
        bridge.bootstrap_workspace()

        conn = brain.init_db()
        brain.add_node("C01", "Test Concept", "concept", group_id=1, conn=conn)
        conn.close()

        brain.reset_domains_cache()
        bridge.bootstrap_workspace()

        conn = brain.init_db()
        node = conn.execute("SELECT label FROM nodes WHERE id='C01'").fetchone()
        conn.close()
        assert node[0] == "Test Concept"


class TestLegacyMigration:
    def test_migrates_loose_files_into_container(self, empty_workspace):
        """Pre-2.0 layout: loose brain.db, AXIOMS.md, GRAPH.json at workspace root.
        Bootstrap should move them into prism/ with the prism- prefix on first run.
        """
        # Simulate pre-2.0 workspace
        (empty_workspace / "inbox").mkdir()
        (empty_workspace / "sources").mkdir()
        (empty_workspace / "extensions").mkdir()
        (empty_workspace / "brain.db").write_bytes(b"")  # marker file; real DB built by bootstrap if missing
        (empty_workspace / "AXIOMS.md").write_text("# legacy axioms")
        (empty_workspace / "GRAPH.json").write_text("{}")

        result = bridge.bootstrap_workspace()
        prism = empty_workspace / "prism"

        # Loose files have moved; root is clean
        for legacy in ["brain.db", "AXIOMS.md", "GRAPH.json", "inbox", "sources", "extensions"]:
            assert not (empty_workspace / legacy).exists(), f"{legacy} should have been migrated"

        # New layout exists with the migrated content
        assert (prism / "prism-axioms.md").read_text() == "# legacy axioms"
        assert (prism / "prism-graph.json").read_text() == "{}"
        assert (prism / "prism-inbox").is_dir()
        assert (prism / "prism-sources").is_dir()
        assert (prism / "prism-extensions").is_dir()

        # Migration was reported in created[]
        migrated_entries = [c for c in result["created"] if c.startswith("migrated:")]
        assert len(migrated_entries) > 0

    def test_migration_is_idempotent(self, empty_workspace):
        """A workspace already on the new layout should not re-migrate or duplicate."""
        bridge.bootstrap_workspace()
        first_count = len(list((empty_workspace / "prism").iterdir()))
        result = bridge.bootstrap_workspace()
        assert result["is_new"] is False
        second_count = len(list((empty_workspace / "prism").iterdir()))
        assert first_count == second_count


class TestBridgeBootstrapCommand:
    def test_bridge_bootstrap_command(self, empty_workspace):
        result = bridge.handle({"command": "bootstrap", "args": {}})
        assert result["is_new"] is True
        assert (empty_workspace / "prism" / "prism-brain.db").exists()

    def test_bridge_bootstrap_via_subprocess(self, empty_workspace):
        import subprocess
        bridge_path = str(Path(__file__).parent.parent / "engine" / "bridge.py")
        request = json.dumps({
            "command": "bootstrap",
            "args": {},
            "workspace": str(empty_workspace),
        })
        env = {**os.environ, "PRISM_EMBEDDING_BACKEND": "tfidf"}
        result = subprocess.run(
            ["python3", bridge_path],
            input=request, capture_output=True, text=True, timeout=30,
            env=env,
        )
        response = json.loads(result.stdout)
        assert response["ok"] is True, f"Bridge error: {response.get('error')}"
        assert response["result"]["is_new"] is True
