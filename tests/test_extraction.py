"""Concept extraction tests: context building, propose/accept/reject lifecycle, correction logging."""

import json
import os
import shutil
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
import brain
import bridge


FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_SOURCES = FIXTURES / "sample-sources"
DOMAINS_TEST = FIXTURES / "domains-test.json"


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    """Workspace with ingested sources."""
    brain.configure(workspace_root=str(tmp_path), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()
    brain.EMBEDDING_BACKEND = "tfidf"

    brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(SAMPLE_SOURCES.glob("*.md")):
        shutil.copy2(str(f), str(brain.SOURCES_DIR / f.name))

    conn = brain.init_db()
    brain.ingest_documents(conn)
    conn.close()

    yield tmp_path
    brain.EMBEDDING_BACKEND = "auto"


@pytest.fixture
def db(workspace):
    conn = brain.init_db()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Concept ID generation
# ---------------------------------------------------------------------------

class TestNextConceptId:
    def test_first_concept(self, db):
        cid = brain.next_concept_id(db)
        assert cid == "C01"

    def test_increments(self, db):
        brain.add_node("C01", "First", "concept", group_id=1, conn=db)
        cid = brain.next_concept_id(db)
        assert cid == "C02"

    def test_handles_gaps(self, db):
        brain.add_node("C01", "First", "concept", group_id=1, conn=db)
        brain.add_node("C05", "Fifth", "concept", group_id=1, conn=db)
        cid = brain.next_concept_id(db)
        assert cid == "C06"


# ---------------------------------------------------------------------------
# Extraction context
# ---------------------------------------------------------------------------

class TestExtractionContext:
    def test_returns_chunks(self, db):
        ctx = brain.get_extraction_context("S01", db)
        assert len(ctx["chunks"]) > 0
        assert "content" in ctx["chunks"][0]

    def test_returns_source_info(self, db):
        ctx = brain.get_extraction_context("S01", db)
        assert ctx["source_id"] == "S01"
        assert "monetary" in ctx["source_title"].lower()

    def test_returns_existing_concepts(self, db):
        brain.add_node("C01", "Test Concept", "concept", group_id=1, conn=db)
        ctx = brain.get_extraction_context("S01", db)
        assert len(ctx["existing_concepts"]) >= 1
        assert ctx["existing_concepts"][0]["id"] == "C01"

    def test_returns_domains(self, db):
        brain.wire_source_to_domains("S01", [1], db)
        ctx = brain.get_extraction_context("S01", db)
        assert len(ctx["existing_domains"]) >= 1

    def test_returns_next_concept_id(self, db):
        ctx = brain.get_extraction_context("S01", db)
        assert ctx["next_concept_id"] == "C01"

    def test_returns_edge_types(self, db):
        brain.add_node("C01", "A", "concept", group_id=1, conn=db)
        brain.add_node("C02", "B", "concept", group_id=1, conn=db)
        brain.add_edge("C01", "C02", "relates_to", conn=db)
        ctx = brain.get_extraction_context("S01", db)
        assert "relates_to" in ctx["edge_types"]

    def test_returns_recent_corrections(self, db):
        brain.log_correction({"action": "test", "reason": "testing"}, db)
        ctx = brain.get_extraction_context("S01", db)
        assert len(ctx["recent_corrections"]) >= 1

    def test_via_bridge(self, workspace):
        result = bridge.handle({
            "command": "extract_context",
            "args": {"source_id": "S01"},
        })
        assert "chunks" in result
        assert "existing_concepts" in result
        assert "next_concept_id" in result


# ---------------------------------------------------------------------------
# Propose concept
# ---------------------------------------------------------------------------

class TestProposeConcept:
    def test_propose_creates_node(self, db):
        result = brain.propose_concept(
            concept_id="C01", label="Central Bank Independence",
            domain_id=1, source_id="S01", conn=db,
        )
        assert result["status"] == "proposed"
        assert result["concept_id"] == "C01"

        node = db.execute("SELECT label, metadata FROM nodes WHERE id='C01'").fetchone()
        assert node[0] == "Central Bank Independence"
        meta = json.loads(node[1])
        assert meta["status"] == "proposed"

    def test_propose_creates_sourced_from_edge(self, db):
        brain.propose_concept("C01", "Test Concept", 1, "S01", conn=db)

        edge = db.execute(
            "SELECT type FROM edges WHERE source_id='S01' AND target_id='C01'"
        ).fetchone()
        assert edge[0] == "sourced_from"

    def test_propose_creates_domain_edge(self, db):
        brain.wire_source_to_domains("S01", [1], db)
        brain.propose_concept("C01", "Test", 1, "S01", conn=db)

        edge = db.execute(
            "SELECT type FROM edges WHERE source_id='D1' AND target_id='C01'"
        ).fetchone()
        assert edge[0] == "contains"

    def test_propose_with_edges(self, db):
        brain.add_node("C01", "Existing", "concept", group_id=1, conn=db)
        result = brain.propose_concept(
            "C02", "New Concept", 1, "S01",
            edges=[{"target": "C01", "type": "relates_to", "label": "connected"}],
            conn=db,
        )
        assert len(result["edges_created"]) == 1
        assert result["edges_created"][0]["target"] == "C01"

    def test_propose_skips_nonexistent_target(self, db):
        result = brain.propose_concept(
            "C01", "Test", 1, "S01",
            edges=[{"target": "C99", "type": "relates_to"}],
            conn=db,
        )
        assert len(result["edges_created"]) == 0

    def test_propose_via_bridge(self, workspace):
        result = bridge.handle({
            "command": "propose_concept",
            "args": {
                "concept_id": "C01",
                "label": "Monetary Policy Transmission",
                "domain_id": 1,
                "source_id": "S01",
                "edges": [],
            },
        })
        assert result["status"] == "proposed"


# ---------------------------------------------------------------------------
# List proposals
# ---------------------------------------------------------------------------

class TestListProposals:
    def test_empty_list(self, db):
        proposals = brain.list_proposals(db)
        assert proposals == []

    def test_lists_proposed_only(self, db):
        brain.propose_concept("C01", "Proposed", 1, "S01", conn=db)
        brain.add_node("C02", "Permanent", "concept", group_id=1, conn=db)

        proposals = brain.list_proposals(db)
        assert len(proposals) == 1
        assert proposals[0]["id"] == "C01"

    def test_includes_edges(self, db):
        brain.add_node("C01", "Existing", "concept", group_id=1, conn=db)
        brain.propose_concept(
            "C02", "New", 1, "S01",
            edges=[{"target": "C01", "type": "relates_to"}],
            conn=db,
        )
        proposals = brain.list_proposals(db)
        assert len(proposals[0]["edges_out"]) >= 1

    def test_via_bridge(self, workspace):
        bridge.handle({
            "command": "propose_concept",
            "args": {"concept_id": "C01", "label": "Test", "domain_id": 1, "source_id": "S01"},
        })
        result = bridge.handle({"command": "list_proposals", "args": {}})
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Accept proposal
# ---------------------------------------------------------------------------

class TestAcceptProposal:
    def test_accept_removes_proposed_status(self, db):
        brain.propose_concept("C01", "Test", 1, "S01", conn=db)
        result = brain.accept_proposal("C01", db)
        assert result["status"] == "accepted"

        meta = json.loads(
            db.execute("SELECT metadata FROM nodes WHERE id='C01'").fetchone()[0]
        )
        assert "status" not in meta
        assert "accepted_at" in meta

    def test_accept_preserves_edges(self, db):
        brain.propose_concept("C01", "Test", 1, "S01", conn=db)
        brain.accept_proposal("C01", db)

        edges = db.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id='S01' AND target_id='C01'"
        ).fetchone()[0]
        assert edges == 1

    def test_accept_nonexistent_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            brain.accept_proposal("C99", db)

    def test_accepted_not_in_proposals(self, db):
        brain.propose_concept("C01", "Test", 1, "S01", conn=db)
        brain.accept_proposal("C01", db)
        proposals = brain.list_proposals(db)
        assert len(proposals) == 0

    def test_via_bridge(self, workspace):
        bridge.handle({
            "command": "propose_concept",
            "args": {"concept_id": "C01", "label": "Test", "domain_id": 1, "source_id": "S01"},
        })
        result = bridge.handle({"command": "accept_proposal", "args": {"concept_id": "C01"}})
        assert result["status"] == "accepted"


# ---------------------------------------------------------------------------
# Reject proposal
# ---------------------------------------------------------------------------

class TestRejectProposal:
    def test_reject_deletes_node(self, db):
        brain.propose_concept("C01", "Test", 1, "S01", conn=db)
        result = brain.reject_proposal("C01", reason="not relevant", conn=db)
        assert result["status"] == "rejected"

        node = db.execute("SELECT id FROM nodes WHERE id='C01'").fetchone()
        assert node is None

    def test_reject_deletes_edges(self, db):
        brain.propose_concept("C01", "Test", 1, "S01", conn=db)
        brain.reject_proposal("C01", conn=db)

        edges = db.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id='C01' OR target_id='C01'"
        ).fetchone()[0]
        assert edges == 0

    def test_reject_logs_correction(self, db):
        brain.propose_concept("C01", "Bad Concept", 1, "S01", conn=db)
        brain.reject_proposal("C01", reason="too vague", conn=db)

        log_row = db.execute("SELECT value FROM meta WHERE key='correction_log'").fetchone()
        log = json.loads(log_row[0])
        assert len(log) >= 1
        assert log[-1]["action"] == "reject_concept"
        assert log[-1]["label"] == "Bad Concept"
        assert log[-1]["reason"] == "too vague"

    def test_reject_nonexistent_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            brain.reject_proposal("C99", conn=db)

    def test_via_bridge(self, workspace):
        bridge.handle({
            "command": "propose_concept",
            "args": {"concept_id": "C01", "label": "Test", "domain_id": 1, "source_id": "S01"},
        })
        result = bridge.handle({
            "command": "reject_proposal",
            "args": {"concept_id": "C01", "reason": "duplicate"},
        })
        assert result["status"] == "rejected"


# ---------------------------------------------------------------------------
# Correction log
# ---------------------------------------------------------------------------

class TestCorrectionLog:
    def test_log_correction(self, db):
        brain.log_correction({"action": "test", "detail": "testing"}, db)

        row = db.execute("SELECT value FROM meta WHERE key='correction_log'").fetchone()
        log = json.loads(row[0])
        assert len(log) == 1
        assert log[0]["action"] == "test"

    def test_log_appends(self, db):
        brain.log_correction({"action": "first"}, db)
        brain.log_correction({"action": "second"}, db)

        row = db.execute("SELECT value FROM meta WHERE key='correction_log'").fetchone()
        log = json.loads(row[0])
        assert len(log) == 2

    def test_corrections_in_extraction_context(self, db):
        brain.log_correction({"action": "reject", "label": "Bad Idea"}, db)
        ctx = brain.get_extraction_context("S01", db)
        assert len(ctx["recent_corrections"]) >= 1
        assert ctx["recent_corrections"][-1]["label"] == "Bad Idea"


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    def test_propose_accept_lifecycle(self, db):
        brain.propose_concept("C01", "Interest Rate Transmission", 1, "S01", conn=db)
        brain.propose_concept("C02", "Quantitative Easing Effects", 1, "S01",
                            edges=[{"target": "C01", "type": "relates_to"}], conn=db)

        proposals = brain.list_proposals(db)
        assert len(proposals) == 2

        brain.accept_proposal("C01", db)
        brain.reject_proposal("C02", reason="too broad", conn=db)

        proposals = brain.list_proposals(db)
        assert len(proposals) == 0

        c01 = db.execute("SELECT id FROM nodes WHERE id='C01'").fetchone()
        assert c01 is not None

        c02 = db.execute("SELECT id FROM nodes WHERE id='C02'").fetchone()
        assert c02 is None

        log_row = db.execute("SELECT value FROM meta WHERE key='correction_log'").fetchone()
        log = json.loads(log_row[0])
        assert any(e["label"] == "Quantitative Easing Effects" for e in log)
