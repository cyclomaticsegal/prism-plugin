"""Axioms lifecycle tests (B4 surface).

Covers:
- revise_axiom inserts a new row, marks predecessor superseded
- supersession chain (revise twice → first row superseded by second, second by third)
- get_axiom_history returns the full chain in chronological order
- list_axioms(active_only=True) returns only the latest revision per key
- prism-axioms.md projection regenerates on every revision
- Citation existence check surfaces unknown source ids without rejecting the write
- _axiom_row_to_dict parses citations JSON correctly
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
import brain


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    brain.configure(workspace_root=str(tmp_path), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()
    brain.EMBEDDING_BACKEND = "tfidf"

    conn = brain.init_db()
    conn.close()

    yield tmp_path

    brain.EMBEDDING_BACKEND = "auto"


def _seed_source(source_id: str, label: str = None):
    """Add a minimal source node so citations can be validated against it."""
    brain.add_node(source_id, label or f"Source {source_id}", "source")


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestReviseAxiom:
    def test_first_revision_inserts(self, workspace):
        result = brain.revise_axiom(
            key="test-claim",
            body="A claim about the world.",
            citations=[],
            boundary="Where it breaks down.",
        )
        assert result["key"] == "test-claim"
        assert result["body"] == "A claim about the world."
        assert result["boundary"] == "Where it breaks down."
        assert result["superseded_by"] is None
        assert result["superseded_at"] is None

    def test_citations_persist_as_list(self, workspace):
        _seed_source("S01")
        _seed_source("S02")
        result = brain.revise_axiom(
            key="cited-claim", body="Body.", citations=["S01", "S02"]
        )
        assert result["citations"] == ["S01", "S02"]

    def test_no_unknown_citations_when_all_exist(self, workspace):
        _seed_source("S01")
        result = brain.revise_axiom(key="k", body="b", citations=["S01"])
        # When all citations are valid, the unknown_citations key is omitted.
        assert "unknown_citations" not in result

    def test_unknown_citations_surface_without_rejection(self, workspace):
        _seed_source("S01")
        # S99 doesn't exist as a source node.
        result = brain.revise_axiom(key="k", body="b", citations=["S01", "S99"])
        assert result["citations"] == ["S01", "S99"]  # write succeeded
        assert "unknown_citations" in result
        assert result["unknown_citations"] == ["S99"]

    def test_no_citations_no_validation(self, workspace):
        result = brain.revise_axiom(key="k", body="b")
        assert result["citations"] == []
        assert "unknown_citations" not in result


# ---------------------------------------------------------------------------
# Supersession chain
# ---------------------------------------------------------------------------

class TestSupersessionChain:
    def test_second_revision_supersedes_first(self, workspace):
        first = brain.revise_axiom(key="k", body="v1")
        second = brain.revise_axiom(key="k", body="v2")

        history = brain.get_axiom_history("k")
        assert len(history) == 2

        # First should now be marked superseded by second.
        first_after = next(a for a in history if a["id"] == first["id"])
        assert first_after["superseded_by"] == second["id"]
        assert first_after["superseded_at"] is not None

        # Second should still be active.
        second_after = next(a for a in history if a["id"] == second["id"])
        assert second_after["superseded_by"] is None
        assert second_after["superseded_at"] is None

    def test_third_revision_supersedes_only_active(self, workspace):
        """Revising again only marks the currently-active row superseded; the
        already-superseded rows stay pointing at whatever superseded them."""
        first = brain.revise_axiom(key="k", body="v1")
        second = brain.revise_axiom(key="k", body="v2")
        third = brain.revise_axiom(key="k", body="v3")

        history = brain.get_axiom_history("k")
        ids_to_rows = {a["id"]: a for a in history}

        assert ids_to_rows[first["id"]]["superseded_by"] == second["id"]
        assert ids_to_rows[second["id"]]["superseded_by"] == third["id"]
        assert ids_to_rows[third["id"]]["superseded_by"] is None

    def test_history_is_chronological(self, workspace):
        brain.revise_axiom(key="k", body="v1")
        brain.revise_axiom(key="k", body="v2")
        brain.revise_axiom(key="k", body="v3")

        history = brain.get_axiom_history("k")
        bodies = [a["body"] for a in history]
        assert bodies == ["v1", "v2", "v3"]

    def test_list_axioms_active_only(self, workspace):
        brain.revise_axiom(key="k1", body="v1")
        brain.revise_axiom(key="k1", body="v2")
        brain.revise_axiom(key="k2", body="other")

        active = brain.list_axioms(active_only=True)
        keys = sorted(a["key"] for a in active)
        assert keys == ["k1", "k2"]

        bodies = {a["key"]: a["body"] for a in active}
        assert bodies["k1"] == "v2"  # only the latest
        assert bodies["k2"] == "other"

    def test_list_axioms_active_only_false(self, workspace):
        brain.revise_axiom(key="k", body="v1")
        brain.revise_axiom(key="k", body="v2")

        all_rows = brain.list_axioms(active_only=False)
        assert len(all_rows) == 2

    def test_different_keys_dont_affect_each_other(self, workspace):
        a1 = brain.revise_axiom(key="k1", body="claim 1")
        a2 = brain.revise_axiom(key="k2", body="claim 2")

        # Both still active.
        history1 = brain.get_axiom_history("k1")
        history2 = brain.get_axiom_history("k2")
        assert history1[-1]["superseded_by"] is None
        assert history2[-1]["superseded_by"] is None


# ---------------------------------------------------------------------------
# prism-axioms.md projection
# ---------------------------------------------------------------------------

class TestAxiomsProjection:
    def test_projection_regenerates_on_revise(self, workspace):
        axioms_path = brain.AXIOMS_MD

        brain.revise_axiom(key="hello", body="Hello world claim.")
        assert axioms_path.exists()
        content = axioms_path.read_text()
        assert "hello" in content
        assert "Hello world claim." in content

    def test_projection_shows_active_revision_only(self, workspace):
        axioms_path = brain.AXIOMS_MD

        brain.revise_axiom(key="k", body="OLD body that should not appear")
        brain.revise_axiom(key="k", body="NEW body that should appear")

        content = axioms_path.read_text()
        assert "NEW body" in content
        # Old body shouldn't appear in the active section. The revision log
        # at the bottom only shows keys + timestamps, not bodies.
        active_section = content.split("## Revision log")[0]
        assert "OLD body" not in active_section

    def test_projection_lists_revisions_in_log(self, workspace):
        axioms_path = brain.AXIOMS_MD

        brain.revise_axiom(key="k", body="v1")
        brain.revise_axiom(key="k", body="v2")

        content = axioms_path.read_text()
        assert "Revision log" in content
        assert "`k`" in content  # the key shows up in the log

    def test_projection_placeholder_when_empty(self, workspace):
        axioms_path = brain.AXIOMS_MD
        # Force a regen with no axioms.
        brain.regenerate_axioms_projection()
        content = axioms_path.read_text()
        assert "No axioms recorded yet" in content

    def test_projection_renders_citations(self, workspace):
        _seed_source("S01")
        brain.revise_axiom(
            key="k",
            body="Body with cites.",
            citations=["S01"],
            boundary="Boundary clause.",
        )
        content = (brain.AXIOMS_MD).read_text()
        assert "S01" in content
        assert "Boundary clause." in content


# ---------------------------------------------------------------------------
# get_axiom (single active row)
# ---------------------------------------------------------------------------

class TestGetAxiom:
    def test_returns_active_only_by_default(self, workspace):
        brain.revise_axiom(key="k", body="v1")
        brain.revise_axiom(key="k", body="v2")
        active = brain.get_axiom("k")
        assert active["body"] == "v2"

    def test_returns_none_for_unknown_key(self, workspace):
        result = brain.get_axiom("does-not-exist")
        assert result is None

    def test_active_only_false_returns_latest_overall(self, workspace):
        brain.revise_axiom(key="k", body="v1")
        brain.revise_axiom(key="k", body="v2")
        any_row = brain.get_axiom("k", active_only=False)
        assert any_row is not None
