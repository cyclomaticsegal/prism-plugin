"""Classifier tests: domain matching, confidence scores, multi-domain assignment.

The `domains` table in prism-brain.db is the single source of truth.
The fixture seeds it from `fixtures/domains-test.json` directly via upsert_domain,
matching how a real install populates the table (folder hints, prompt context,
or explicit prism_core_domains_set calls). The legacy file-fallback path was removed
in B3; tests that previously exercised it now seed the table.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
import brain


FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_SOURCES = FIXTURES / "sample-sources"
DOMAINS_TEST = FIXTURES / "domains-test.json"


def _seed_domains(workspace_path):
    """Populate the domains table from the test fixture JSON."""
    rows = json.loads(DOMAINS_TEST.read_text())
    for d in rows:
        brain.upsert_domain(
            label=d["label"],
            keywords=d["keywords"],
            short_label=d.get("short_label"),
            color=d.get("color"),
        )
    brain.reset_domains_cache()


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    """Create a fresh workspace for each test."""
    brain.configure(workspace_root=str(tmp_path), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()
    brain.EMBEDDING_BACKEND = "tfidf"

    brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(SAMPLE_SOURCES.glob("*.md")):
        shutil.copy2(str(f), str(brain.SOURCES_DIR / f.name))

    # Seed the domains table directly (the new authoritative path).
    _seed_domains(tmp_path)

    yield tmp_path

    brain.EMBEDDING_BACKEND = "auto"


@pytest.fixture
def db(workspace):
    conn = brain.init_db()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Domain loading
# ---------------------------------------------------------------------------

class TestDomainLoading:
    def test_load_domains_from_table(self, workspace):
        domains = brain.load_domains()
        assert len(domains) == 5
        assert domains[0]["id"] == 1
        assert domains[0]["label"] == "Economics & Markets"

    def test_load_domains_empty_when_table_empty(self, workspace):
        """Empty domains table → empty list. No hardcoded defaults."""
        # Wipe the seeded table.
        for d in brain.list_domains_table():
            brain.delete_domain(d["id"])
        brain.reset_domains_cache()

        # Even with a stray legacy domains.json sitting in the workspace,
        # load_domains returns []: the file-fallback path was removed in B3.
        (workspace / "domains.json").write_text(json.dumps([
            {"id": 1, "label": "Should Be Ignored", "keywords": "ignored"}
        ]))

        domains = brain.load_domains()
        assert domains == []

    def test_legacy_domains_json_is_ignored(self, workspace):
        """A workspace with both DB rows and a legacy domains.json reads only the DB."""
        (workspace / "domains.json").write_text(json.dumps([
            {"id": 99, "label": "From File", "keywords": "should not load"}
        ]))
        brain.reset_domains_cache()

        domains = brain.load_domains()
        labels = [d["label"] for d in domains]
        assert "From File" not in labels
        assert "Economics & Markets" in labels

    def test_load_domains_cached(self, workspace):
        d1 = brain.load_domains()
        d2 = brain.load_domains()
        assert d1 is d2

    def test_reset_domains_cache(self, workspace):
        d1 = brain.load_domains()
        brain.reset_domains_cache()
        d2 = brain.load_domains()
        assert d1 is not d2

    def test_get_domain_descriptions(self, workspace):
        descs = brain.get_domain_descriptions()
        assert isinstance(descs, dict)
        assert 1 in descs
        assert "macroeconomics" in descs[1].lower()

    def test_get_domain_labels(self, workspace):
        labels = brain.get_domain_labels()
        assert labels[1] == "Economics & Markets"

    def test_get_domain_colors(self, workspace):
        colors = brain.get_domain_colors()
        assert all(c.startswith("#") for c in colors.values())

    def test_upsert_domain_fills_optional_fields(self, workspace):
        """upsert_domain auto-fills short_label and color when omitted."""
        result = brain.upsert_domain(label="Test Domain", keywords="test domain keywords")
        assert result["short_label"] == "Test Domain"
        assert result["color"].startswith("#")

    def test_upsert_domain_idempotent_on_label(self, workspace):
        """upsert_domain by label updates instead of duplicating."""
        before = brain.list_domains_table()
        brain.upsert_domain(label="Economics & Markets", keywords="updated keywords")
        after = brain.list_domains_table()
        assert len(after) == len(before)
        econ = [d for d in after if d["label"] == "Economics & Markets"][0]
        assert econ["keywords"] == "updated keywords"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassification:
    @pytest.fixture(autouse=True)
    def _ingest(self, db, workspace):
        brain.ingest_documents(db)
        db.commit()

    def test_classify_returns_results(self, db):
        results = brain.classify_source_domains("S01", db)
        assert len(results) > 0
        assert "domain_id" in results[0]
        assert "score" in results[0]

    def test_classify_scores_are_floats(self, db):
        results = brain.classify_source_domains("S01", db)
        assert all(isinstance(r["score"], float) for r in results)
        assert all(0 <= r["score"] <= 1 for r in results)

    def test_classify_monetary_policy_as_economics(self, db):
        results = brain.classify_source_domains("S01", db)
        domain_ids = [r["domain_id"] for r in results]
        assert 1 in domain_ids  # Economics

    def test_classify_transformer_as_technology(self, db):
        results = brain.classify_source_domains("S02", db)
        domain_ids = [r["domain_id"] for r in results]
        assert 2 in domain_ids  # Technology

    def test_classify_energy_as_energy(self, db):
        results = brain.classify_source_domains("S03", db)
        domain_ids = [r["domain_id"] for r in results]
        assert 3 in domain_ids  # Energy

    def test_classify_philosophy_as_philosophy(self, db):
        results = brain.classify_source_domains("S04", db)
        domain_ids = [r["domain_id"] for r in results]
        assert 4 in domain_ids  # Philosophy

    def test_classify_governance_as_political_economy(self, db):
        results = brain.classify_source_domains("S05", db)
        domain_ids = [r["domain_id"] for r in results]
        # Should match either political economy (5) or technology (2) — it's about AI governance
        assert 5 in domain_ids or 2 in domain_ids

    def test_multi_domain_assignment(self, db):
        results = brain.classify_source_domains("S05", db, top_n=3)
        assert len(results) >= 2

    def test_confidence_threshold(self, db):
        low = brain.classify_source_domains("S01", db, threshold=0.01)
        high = brain.classify_source_domains("S01", db, threshold=0.5)
        assert len(high) <= len(low)

    def test_classify_nonexistent_source(self, db):
        results = brain.classify_source_domains("S99", db)
        assert results == []

    def test_sorted_by_score_descending(self, db):
        results = brain.classify_source_domains("S01", db, top_n=5)
        if len(results) > 1:
            scores = [r["score"] for r in results]
            assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Wire to domains
# ---------------------------------------------------------------------------

class TestWireDomains:
    def test_wire_creates_domain_nodes(self, db, workspace):
        brain.ingest_documents(db)

        actions = brain.wire_source_to_domains("S01", [1, 2], db)
        assert any("Created domain node" in a for a in actions)

        d1 = db.execute("SELECT label FROM nodes WHERE id='D1'").fetchone()
        assert d1 is not None

    def test_wire_creates_edges(self, db, workspace):
        brain.ingest_documents(db)
        brain.wire_source_to_domains("S01", [1], db)

        edge = db.execute(
            "SELECT type FROM edges WHERE source_id='D1' AND target_id='S01'"
        ).fetchone()
        assert edge[0] == "contains"

    def test_wire_sets_group_id(self, db, workspace):
        brain.ingest_documents(db)
        brain.wire_source_to_domains("S01", [3, 1], db)

        group = db.execute("SELECT group_id FROM nodes WHERE id='S01'").fetchone()
        assert group[0] == 3  # First domain in list

    def test_wire_idempotent(self, db, workspace):
        brain.ingest_documents(db)
        brain.wire_source_to_domains("S01", [1], db)
        brain.wire_source_to_domains("S01", [1], db)

        edges = db.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id='D1' AND target_id='S01'"
        ).fetchone()
        assert edges[0] == 1

    def test_wire_uses_domain_labels_from_config(self, db, workspace):
        brain.ingest_documents(db)
        brain.wire_source_to_domains("S01", [1], db)

        label = db.execute("SELECT label FROM nodes WHERE id='D1'").fetchone()[0]
        assert label == "Economics & Markets"  # From test domains.json
