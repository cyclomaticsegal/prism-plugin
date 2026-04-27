"""Multi-modal ingestion tests (B5 surface).

Covers:
- ingest_text: pasted content becomes a source row + chunks + node
- ingest_url: HTTP fetch (mocked), title extraction, classification hint
- folder hints in process_inbox: subfolder names seed/select domains
- the full dispatch surface: prism_core_ingest, prism_core_ingest_url, prism_core_ingest_text
  via the bridge in single-shot mode

URL fetch is mocked at urllib.request.urlopen — these tests never hit the
network. The HTML→text path is exercised through `_strip_html`'s regex
fallback so the tests pass even without bs4/readability installed.
"""

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
import brain
import bridge


@pytest.fixture(autouse=True)
def workspace(tmp_path):
    brain.configure(workspace_root=str(tmp_path), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()
    brain.EMBEDDING_BACKEND = "tfidf"

    brain.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    conn = brain.init_db()
    conn.close()

    yield tmp_path

    brain.EMBEDDING_BACKEND = "auto"


def _make_mock_response(html_bytes: bytes, content_type: str = "text/html; charset=utf-8"):
    """Build a fake urlopen() context-manager response."""
    headers = MagicMock()
    headers.get = MagicMock(side_effect=lambda key, default=None: (
        content_type if key == "Content-Type" else default
    ))

    response = MagicMock()
    response.read.return_value = html_bytes
    response.headers = headers
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


# ---------------------------------------------------------------------------
# ingest_text
# ---------------------------------------------------------------------------

class TestIngestText:
    def test_creates_source_node_and_chunks(self, workspace):
        result = brain.ingest_text(
            text="A passage of test content. " * 200,
            title="My Passage",
        )
        assert result["source_id"].startswith("S")
        assert result["title"] == "My Passage"

        conn = brain.init_db()
        node = conn.execute(
            "SELECT type FROM nodes WHERE id = ?", (result["source_id"],)
        ).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_id = ?", (result["source_id"],)
        ).fetchone()
        conn.close()
        assert node[0] == "source"
        assert chunks[0] >= 1

    def test_writes_file_to_sources_folder(self, workspace):
        result = brain.ingest_text(text="Hello world. " * 100, title="Greeting")
        sources = list((brain.SOURCES_DIR).iterdir())
        assert len(sources) == 1
        assert result["path"].startswith("prism/prism-sources/")
        assert sources[0].name.endswith(".md")
        content = sources[0].read_text()
        assert "Greeting" in content
        assert "Hello world" in content

    def test_metadata_lands_in_file_frontmatter(self, workspace):
        brain.ingest_text(
            text="Content. " * 100,
            title="With Meta",
            metadata={"author": "Test Author", "year": "2026"},
        )
        sources = list((brain.SOURCES_DIR).iterdir())
        content = sources[0].read_text()
        assert "author: Test Author" in content
        assert "year: 2026" in content

    def test_empty_text_raises(self, workspace):
        with pytest.raises(ValueError, match="non-empty"):
            brain.ingest_text(text="", title="Empty")
        with pytest.raises(ValueError, match="non-empty"):
            brain.ingest_text(text="   ", title="Whitespace only")

    def test_default_title_when_omitted(self, workspace):
        result = brain.ingest_text(text="Some text. " * 50)
        assert result["title"].startswith("Pasted source ")

    def test_domain_hint_seeds_new_domain(self, workspace):
        result = brain.ingest_text(
            text="Energy infrastructure content. " * 50,
            title="Hint test",
            domain_hint="energy-infrastructure",
        )
        assert len(result["domains"]) == 1

        # The domain should now exist in the table.
        domains = brain.list_domains_table()
        labels = [d["label"] for d in domains]
        assert "Energy Infrastructure" in labels

    def test_classifier_runs_when_no_hint(self, workspace):
        # Seed a domain; classifier should pick it up.
        brain.upsert_domain(
            label="Test Domain",
            keywords="test content passage testing",
        )
        brain.reset_domains_cache()
        result = brain.ingest_text(
            text="test content passage testing " * 100,
            title="Classifier test",
        )
        # Either the classifier hits or it doesn't — but the call must not raise.
        assert "domains" in result


# ---------------------------------------------------------------------------
# ingest_url (mocked HTTP)
# ---------------------------------------------------------------------------

class TestIngestUrl:
    def test_basic_fetch_and_ingest(self, workspace):
        html = (
            "<html><head><title>Test Article</title></head>"
            "<body><p>" + ("Article body content. " * 100) + "</p></body></html>"
        )
        mock_response = _make_mock_response(html.encode("utf-8"))

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = brain.ingest_url(url="https://example.com/article")

        assert result["source_id"].startswith("S")
        # Title extracted from <title> tag.
        assert "Test Article" in result["title"]

    def test_uses_explicit_title_when_provided(self, workspace):
        html = "<html><head><title>From HTML</title></head><body><p>" + ("body. " * 80) + "</p></body></html>"
        mock_response = _make_mock_response(html.encode("utf-8"))

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = brain.ingest_url(
                url="https://example.com/x",
                title="Explicit Title",
            )

        assert result["title"] == "Explicit Title"

    def test_url_persists_in_metadata(self, workspace):
        html = "<html><body><p>" + ("content. " * 80) + "</p></body></html>"
        mock_response = _make_mock_response(html.encode("utf-8"))

        url = "https://example.com/some-article"
        with patch("urllib.request.urlopen", return_value=mock_response):
            brain.ingest_url(url=url)

        # The URL should appear in the source file's frontmatter.
        sources = list((brain.SOURCES_DIR).iterdir())
        content = sources[0].read_text()
        assert url in content

    def test_fetch_failure_raises(self, workspace):
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            with pytest.raises(RuntimeError, match="Could not fetch"):
                brain.ingest_url(url="https://example.com/x")

    def test_empty_response_raises(self, workspace):
        mock_response = _make_mock_response(b"<html></html>")
        with patch("urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(RuntimeError, match="No text"):
                brain.ingest_url(url="https://example.com/empty")

    def test_domain_hint_honoured(self, workspace):
        html = "<html><body><p>" + ("body. " * 80) + "</p></body></html>"
        mock_response = _make_mock_response(html.encode("utf-8"))

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = brain.ingest_url(
                url="https://example.com/x",
                domain_hint="ai-research",
            )

        domains = brain.list_domains_table()
        labels = [d["label"] for d in domains]
        assert "Ai Research" in labels  # title-cased from the slug
        assert len(result["domains"]) == 1


# ---------------------------------------------------------------------------
# Folder hints in process_inbox
# ---------------------------------------------------------------------------

class TestFolderHintIngestion:
    def test_subfolder_seeds_domain(self, workspace):
        # Drop a file under inbox/network-effects/.
        sub = brain.INBOX_DIR / "network-effects"
        sub.mkdir()
        (sub / "article.md").write_text("# Article\n\n" + ("Network effects content. " * 100))

        processed = brain.process_inbox()
        assert len(processed) == 1
        assert processed[0]["folder_hint"] == "network-effects"

        domains = brain.list_domains_table()
        labels = [d["label"] for d in domains]
        assert "Network Effects" in labels

    def test_top_level_files_have_no_folder_hint(self, workspace):
        (brain.INBOX_DIR / "loose.md").write_text(
            "# Loose\n\n" + ("Top-level content. " * 100)
        )
        processed = brain.process_inbox()
        assert len(processed) == 1
        assert processed[0]["folder_hint"] is None

    def test_folder_hint_overrides_classifier(self, workspace):
        # Seed a domain that the classifier would otherwise select.
        brain.upsert_domain(
            label="Auto Classified",
            keywords="energy infrastructure power grid generation",
        )
        brain.reset_domains_cache()

        sub = brain.INBOX_DIR / "manual-override"
        sub.mkdir()
        (sub / "energy.md").write_text(
            "# Energy\n\n" + ("Energy infrastructure power grid generation. " * 80)
        )

        processed = brain.process_inbox()
        assert processed[0]["domains"]
        # The folder hint domain ("Manual Override") should be the wired one.
        wired_domain_id = processed[0]["domains"][0]
        domains = brain.list_domains_table()
        wired_label = [d["label"] for d in domains if d["id"] == wired_domain_id][0]
        assert wired_label == "Manual Override"

    def test_multiple_subfolders_seed_multiple_domains(self, workspace):
        # Distinct vocabularies per folder so the TF-IDF embedder has terms
        # surviving max_df=0.95 pruning across the small 3-document corpus.
        bodies = {
            "energy-policy": "renewable energy grid infrastructure transmission solar nuclear ",
            "ai-research": "neural networks transformer attention scaling compute parameters ",
            "monetary-systems": "central banking interest rate inflation currency reserves ",
        }
        for folder, body in bodies.items():
            sub = brain.INBOX_DIR / folder
            sub.mkdir()
            (sub / f"{folder}.md").write_text(
                f"# {folder}\n\n" + (body * 50)
            )

        brain.process_inbox()

        labels = [d["label"] for d in brain.list_domains_table()]
        assert "Energy Policy" in labels
        assert "Ai Research" in labels
        assert "Monetary Systems" in labels


# ---------------------------------------------------------------------------
# Bridge command surface (single-shot)
# ---------------------------------------------------------------------------

class TestBridgeIngestionCommands:
    """Drive the new commands through bridge.handle to lock in the dispatch surface."""

    def test_ingest_text_via_bridge(self, workspace):
        result = bridge.handle({
            "command": "ingest_text",
            "args": {
                "text": "Bridge-ingested text content. " * 50,
                "title": "Bridge test",
            },
        })
        assert "source_id" in result

    def test_reingest_via_bridge(self, workspace):
        # First ingest something so we have a source to reingest.
        ingest = bridge.handle({
            "command": "ingest_text",
            "args": {"text": "original. " * 80, "title": "ReIngest test"},
        })
        sid = ingest["source_id"]

        # Edit the file on disk so reingest sees new content.
        source_file = next((brain.SOURCES_DIR).iterdir())
        source_file.write_text("# Updated\n\n" + ("brand new. " * 80))

        result = bridge.handle({"command": "reingest", "args": {"source_id": sid}})
        assert result["source_id"] == sid
        assert result["status"] == "reingested"

    def test_source_delete_via_bridge(self, workspace):
        ingest = bridge.handle({
            "command": "ingest_text",
            "args": {"text": "to be deleted. " * 80, "title": "Delete test"},
        })
        sid = ingest["source_id"]

        result = bridge.handle({"command": "source_delete", "args": {"source_id": sid}})
        assert result["deleted"] == 1
