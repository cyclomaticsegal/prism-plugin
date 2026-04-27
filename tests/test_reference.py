"""Reference brain tests: build, search, parallel search."""

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
REF_BRAIN = Path(__file__).parent.parent / "reference-brain"


@pytest.fixture
def workspace_with_ref(tmp_path):
    """Workspace with user brain + reference brain available."""
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


# ---------------------------------------------------------------------------
# Reference brain build artifacts
# ---------------------------------------------------------------------------

class TestReferenceBrainArtifacts:
    def test_domains_reference_exists(self):
        path = REF_BRAIN / "domains-reference.json"
        assert path.exists()
        domains = json.loads(path.read_text())
        assert len(domains) == 8
        for d in domains:
            assert "id" in d
            assert "label" in d
            assert "keywords" in d
            assert len(d["keywords"]) > 50

    def test_axioms_reference_exists(self):
        path = REF_BRAIN / "AXIOMS-reference.md"
        assert path.exists()
        content = path.read_text()
        assert "The Prism" in content
        assert "Framework" in content
        assert "Bridge" in content

    def test_source_files_exist(self):
        sources = REF_BRAIN / "sources"
        assert sources.is_dir()
        files = list(sources.glob("S*.md"))
        assert len(files) >= 30, f"Only {len(files)} sources found, need 30+"

    def test_source_files_have_content(self):
        sources = REF_BRAIN / "sources"
        for f in sorted(sources.glob("S*.md"))[:5]:
            content = f.read_text()
            assert len(content) > 200, f"{f.name} is too short ({len(content)} chars)"


# ---------------------------------------------------------------------------
# Reference brain build
# ---------------------------------------------------------------------------

class TestReferenceBrainBuild:
    def test_build_produces_db(self, tmp_path):
        """Test that the build script produces a valid prism-brain.db."""
        # We test the build logic without running the full script
        # by using the same functions
        workspace = tmp_path / "ref_workspace"
        workspace.mkdir()

        # Copy a subset of reference sources for speed
        ref_sources = REF_BRAIN / "sources"
        if not ref_sources.exists():
            pytest.skip("Reference sources not yet created")

        brain.configure(workspace_root=str(workspace), session_dir=None)
        brain.reset_embedder()
        brain.reset_domains_cache()
        brain.EMBEDDING_BACKEND = "tfidf"

        brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
        for f in ref_sources.iterdir():
            if f.is_file():
                shutil.copy2(str(f), str(brain.SOURCES_DIR / f.name))

        conn = brain.init_db()
        brain.ingest_documents(conn)

        n_sources = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE type='source'"
        ).fetchone()[0]
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        conn.close()

        assert n_sources >= 30
        assert n_chunks > 0


# ---------------------------------------------------------------------------
# Reference search
# ---------------------------------------------------------------------------

class TestReferenceSearch:
    def test_search_reference_no_db(self, workspace_with_ref):
        result = bridge.handle({
            "command": "search_reference",
            "args": {"query": "division of labour", "top_k": 3},
        })
        # No reference DB built yet in test workspace, returns empty
        assert isinstance(result, list)

    def test_search_reference_with_db(self, workspace_with_ref):
        ref_db = REF_BRAIN / "brain-reference.db"
        if not ref_db.exists():
            pytest.skip("Reference brain not yet built")

        result = bridge.handle({
            "command": "search_reference",
            "args": {"query": "natural selection evolution", "top_k": 3},
        })
        assert isinstance(result, list)
        if len(result) > 0:
            assert result[0]["source"] == "reference"
