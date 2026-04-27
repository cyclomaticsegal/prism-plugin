"""Source lifecycle tests (B6 surface).

Covers:
- reingest_source: re-reads file, regenerates chunks/embeddings, preserves
  the source node and its existing graph wiring (domain edges)
- delete_source: removes node, chunks, embeddings, all edges; leaves the
  file on disk untouched
- error paths: reingest of unknown id, reingest when file missing, delete
  of unknown id

The contract is "the database owns content; sources/ is a read-only archive."
These tests verify the boundary holds.
"""

import shutil
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

    brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    brain.INBOX_DIR.mkdir(parents=True, exist_ok=True)

    conn = brain.init_db()
    conn.close()

    yield tmp_path

    brain.EMBEDDING_BACKEND = "auto"


def _ingest_one(workspace, body: str = "Test content. " * 200, title: str = "Test"):
    """Helper: drop a markdown file in inbox/ and run process_inbox. Returns source_id."""
    (brain.INBOX_DIR / f"{title.lower()}.md").write_text(f"# {title}\n\n{body}\n")
    processed = brain.process_inbox()
    assert len(processed) == 1
    return processed[0]["source_id"]


# ---------------------------------------------------------------------------
# Reingest
# ---------------------------------------------------------------------------

class TestReingestSource:
    def test_reingest_unknown_id_raises(self, workspace):
        with pytest.raises(ValueError, match="not found"):
            brain.reingest_source("S99")

    def test_reingest_preserves_source_node(self, workspace):
        sid = _ingest_one(workspace)

        # Find the on-disk file and edit it.
        sources = list((brain.SOURCES_DIR).iterdir())
        assert len(sources) == 1
        source_file = sources[0]
        source_file.write_text("# Updated\n\n" + ("Brand new content. " * 200))

        result = brain.reingest_source(sid)
        assert result["source_id"] == sid
        assert result["status"] == "reingested"

        # Source node still exists.
        conn = brain.init_db()
        node = conn.execute(
            "SELECT id, type FROM nodes WHERE id = ?", (sid,)
        ).fetchone()
        conn.close()
        assert node is not None
        assert node[1] == "source"

    def test_reingest_replaces_chunks(self, workspace):
        sid = _ingest_one(workspace, body="Original content. " * 200)

        conn = brain.init_db()
        original_chunks = conn.execute(
            "SELECT content FROM chunks WHERE source_id = ?", (sid,)
        ).fetchall()
        conn.close()
        assert any("Original content" in c[0] for c in original_chunks)

        # Edit on disk.
        source_file = next((brain.SOURCES_DIR).iterdir())
        source_file.write_text("# Updated\n\n" + ("Replacement content. " * 200))

        brain.reingest_source(sid)

        conn = brain.init_db()
        new_chunks = conn.execute(
            "SELECT content FROM chunks WHERE source_id = ?", (sid,)
        ).fetchall()
        conn.close()
        assert any("Replacement content" in c[0] for c in new_chunks)
        assert not any("Original content" in c[0] for c in new_chunks)

    def test_reingest_preserves_graph_wiring(self, workspace):
        # Seed a domain and wire the source to it.
        brain.upsert_domain(label="Test Domain", keywords="test")
        sid = _ingest_one(workspace)

        conn = brain.init_db()
        brain.wire_source_to_domains(sid, [1], conn)
        conn.close()

        conn = brain.init_db()
        edges_before = conn.execute(
            "SELECT source_id, target_id, type FROM edges "
            "WHERE source_id = ? OR target_id = ?",
            (sid, sid),
        ).fetchall()
        conn.close()
        assert len(edges_before) > 0

        # Edit and reingest.
        source_file = next((brain.SOURCES_DIR).iterdir())
        source_file.write_text("# Updated\n\n" + ("New content. " * 200))
        brain.reingest_source(sid)

        # Domain edges still there.
        conn = brain.init_db()
        edges_after = conn.execute(
            "SELECT source_id, target_id, type FROM edges "
            "WHERE source_id = ? OR target_id = ?",
            (sid, sid),
        ).fetchall()
        conn.close()
        # At minimum the original domain edge survives.
        assert len(edges_after) >= len(edges_before)

    def test_reingest_missing_file_raises(self, workspace):
        sid = _ingest_one(workspace)

        # Delete the file from sources/.
        source_file = next((brain.SOURCES_DIR).iterdir())
        source_file.unlink()

        with pytest.raises(FileNotFoundError):
            brain.reingest_source(sid)


# ---------------------------------------------------------------------------
# Delete source
# ---------------------------------------------------------------------------

class TestDeleteSource:
    def test_delete_unknown_id_returns_zero(self, workspace):
        result = brain.delete_source("S99")
        assert result["deleted"] == 0
        assert result["chunks_removed"] == 0

    def test_delete_removes_node(self, workspace):
        sid = _ingest_one(workspace)
        result = brain.delete_source(sid)
        assert result["deleted"] == 1

        conn = brain.init_db()
        node = conn.execute("SELECT id FROM nodes WHERE id = ?", (sid,)).fetchone()
        conn.close()
        assert node is None

    def test_delete_removes_chunks_and_embeddings(self, workspace):
        sid = _ingest_one(workspace)

        conn = brain.init_db()
        chunks_before = conn.execute(
            "SELECT id FROM chunks WHERE source_id = ?", (sid,)
        ).fetchall()
        chunk_ids = [c[0] for c in chunks_before]
        conn.close()
        assert len(chunk_ids) > 0

        result = brain.delete_source(sid)
        assert result["chunks_removed"] == len(chunk_ids)

        conn = brain.init_db()
        chunks_after = conn.execute(
            "SELECT id FROM chunks WHERE source_id = ?", (sid,)
        ).fetchall()
        if chunk_ids:
            placeholders = ",".join("?" * len(chunk_ids))
            embeddings_after = conn.execute(
                f"SELECT chunk_id FROM embeddings WHERE chunk_id IN ({placeholders})",
                chunk_ids,
            ).fetchall()
        else:
            embeddings_after = []
        conn.close()
        assert chunks_after == []
        assert embeddings_after == []

    def test_delete_removes_edges(self, workspace):
        brain.upsert_domain(label="Test Domain", keywords="test")
        sid = _ingest_one(workspace)

        conn = brain.init_db()
        brain.wire_source_to_domains(sid, [1], conn)
        conn.close()

        conn = brain.init_db()
        edges_before = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? OR target_id = ?",
            (sid, sid),
        ).fetchall()
        conn.close()
        assert len(edges_before) > 0

        brain.delete_source(sid)

        conn = brain.init_db()
        edges_after = conn.execute(
            "SELECT id FROM edges WHERE source_id = ? OR target_id = ?",
            (sid, sid),
        ).fetchall()
        conn.close()
        assert edges_after == []

    def test_delete_does_not_remove_file(self, workspace):
        """The contract: sources/ is a read-only archive; deletion is DB-only."""
        sid = _ingest_one(workspace)

        source_files_before = list((brain.SOURCES_DIR).iterdir())
        assert len(source_files_before) == 1
        source_file = source_files_before[0]

        brain.delete_source(sid)

        # File survives.
        assert source_file.exists()
        assert (brain.SOURCES_DIR).exists()
        source_files_after = list((brain.SOURCES_DIR).iterdir())
        assert source_files_after == source_files_before


# ---------------------------------------------------------------------------
# Round-trip: delete then reingest the file via process_inbox
# ---------------------------------------------------------------------------

class TestSourceLifecycleRoundtrip:
    def test_delete_then_reingest_via_inbox(self, workspace):
        sid = _ingest_one(workspace)

        # Move the file out of sources/ before deleting from DB so we can
        # re-introduce it through inbox/ afterwards.
        source_file = next((brain.SOURCES_DIR).iterdir())
        archived = workspace / "archived.md"
        shutil.copy2(str(source_file), str(archived))

        brain.delete_source(sid)

        # The original file is still on disk in sources/.
        assert source_file.exists()

        # Move a fresh copy through inbox/ and ingest. Should get a NEW id
        # because next_source_id derives from the existing files in sources/.
        shutil.copy2(str(archived), str(brain.INBOX_DIR / "fresh.md"))
        processed = brain.process_inbox()
        assert len(processed) == 1
        new_sid = processed[0]["source_id"]
        assert new_sid != sid

        conn = brain.init_db()
        node = conn.execute(
            "SELECT id FROM nodes WHERE id = ?", (new_sid,)
        ).fetchone()
        conn.close()
        assert node is not None
