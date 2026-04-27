"""Extension architecture tests: discovery, restricted API, install/remove lifecycle.

The loader expects extensions at <workspace>/prism/prism-extensions/<ext_id>/
and rewrites every declared tool name to prism_<ext_id>_<bare_name> at
registration time. Manifest contract:
  - "name" is the extension id, must match ^[a-z][a-z0-9]*$
  - "tools" declares bare names (no prism_ext_ prefix)
"""

import json
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
EXTENSIONS_SRC = Path(__file__).parent.parent / "extensions"


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


# ---------------------------------------------------------------------------
# Restricted API bridge commands
# ---------------------------------------------------------------------------

class TestAddNodeBridge:
    def test_add_node(self, workspace):
        result = bridge.handle({
            "command": "add_node",
            "args": {"node_id": "C01", "label": "Test Concept", "node_type": "concept", "group_id": 1},
        })
        assert result["node_id"] == "C01"
        assert result["inserted"] is True

    def test_add_node_duplicate(self, workspace):
        bridge.handle({
            "command": "add_node",
            "args": {"node_id": "C01", "label": "First", "node_type": "concept"},
        })
        result = bridge.handle({
            "command": "add_node",
            "args": {"node_id": "C01", "label": "Updated", "node_type": "concept"},
        })
        assert result["inserted"] is False


class TestAddEdgeBridge:
    def test_add_edge(self, workspace):
        bridge.handle({"command": "add_node", "args": {"node_id": "C01", "label": "A", "node_type": "concept"}})
        bridge.handle({"command": "add_node", "args": {"node_id": "C02", "label": "B", "node_type": "concept"}})
        result = bridge.handle({
            "command": "add_edge",
            "args": {"source_id": "C01", "target_id": "C02", "edge_type": "relates_to", "label": "test"},
        })
        assert result["inserted"] is True

    def test_add_edge_duplicate(self, workspace):
        bridge.handle({"command": "add_node", "args": {"node_id": "C01", "label": "A", "node_type": "concept"}})
        bridge.handle({"command": "add_node", "args": {"node_id": "C02", "label": "B", "node_type": "concept"}})
        bridge.handle({"command": "add_edge", "args": {"source_id": "C01", "target_id": "C02", "edge_type": "relates_to"}})
        result = bridge.handle({"command": "add_edge", "args": {"source_id": "C01", "target_id": "C02", "edge_type": "relates_to"}})
        assert result["inserted"] is False


class TestGetGraphDataBridge:
    def test_get_graph_data(self, workspace):
        result = bridge.handle({"command": "get_graph_data", "args": {}})
        assert "nodes" in result
        assert "edges" in result
        assert "meta" in result
        assert result["meta"]["title"] == "PRISM — Knowledge Graph"


# ---------------------------------------------------------------------------
# Extension manifest validation
# ---------------------------------------------------------------------------

class TestExtensionManifest:
    def test_folds_manifest_valid(self):
        manifest_path = EXTENSIONS_SRC / "51folds" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        # Extension id must be lowercase, no underscores (loader contract)
        assert manifest["name"] == "folds"
        # Tools declared as bare names — loader stamps prism_folds_<name>
        assert "tools" in manifest
        tool_names = {t["name"] for t in manifest["tools"]}
        assert {"refine_thesis", "create", "status", "ingest_results"} <= tool_names

    def test_folds_env_vars_namespaced(self):
        manifest = json.loads((EXTENSIONS_SRC / "51folds" / "manifest.json").read_text())
        for k in manifest.get("env", {}):
            assert k.startswith("PRISM_FOLDS_"), f"env var {k} must use PRISM_FOLDS_ prefix"

    def test_folds_tools_exists(self):
        tools_path = EXTENSIONS_SRC / "51folds" / "tools.js"
        assert tools_path.exists()
        content = tools_path.read_text()
        assert "export default function register" in content


# ---------------------------------------------------------------------------
# Extension install/remove lifecycle
# ---------------------------------------------------------------------------

class TestExtensionLifecycle:
    def _install_extension(self, workspace, ext_name="51folds"):
        """Copy an extension into the workspace's prism/prism-extensions/ directory."""
        ext_src = EXTENSIONS_SRC / ext_name
        ext_dest = brain.EXTENSIONS_DIR / ext_name
        ext_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(ext_src), str(ext_dest))
        return ext_dest

    def test_discover_installed_extension(self, workspace):
        self._install_extension(workspace)
        ext_dir = brain.EXTENSIONS_DIR
        manifests = []
        for d in ext_dir.iterdir():
            if d.is_dir():
                mp = d / "manifest.json"
                if mp.exists():
                    manifests.append(json.loads(mp.read_text()))
        assert len(manifests) == 1
        assert manifests[0]["name"] == "folds"

    def test_discover_no_extensions(self, workspace):
        ext_dir = brain.EXTENSIONS_DIR
        ext_dir.mkdir(parents=True, exist_ok=True)
        manifests = []
        for d in ext_dir.iterdir():
            if d.is_dir() and (d / "manifest.json").exists():
                manifests.append(json.loads((d / "manifest.json").read_text()))
        assert len(manifests) == 0

    def test_remove_extension_leaves_core(self, workspace):
        ext_path = self._install_extension(workspace)

        conn = brain.init_db()
        n_before = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()

        shutil.rmtree(str(ext_path))

        conn = brain.init_db()
        n_after = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()

        assert n_after == n_before

    def test_replace_extension(self, workspace):
        ext_path = self._install_extension(workspace)

        manifest = json.loads((ext_path / "manifest.json").read_text())
        original_version = manifest["version"]

        manifest["version"] = "9.9.9"
        (ext_path / "manifest.json").write_text(json.dumps(manifest, indent=2))

        updated = json.loads((ext_path / "manifest.json").read_text())
        assert updated["version"] == "9.9.9"
        assert updated["version"] != original_version


# ---------------------------------------------------------------------------
# Extension tool functionality (via bridge, simulating what the server does)
# ---------------------------------------------------------------------------

class TestExtensionTools:
    def test_prism_folds_create_via_bridge(self, workspace):
        result = bridge.handle({
            "command": "add_node",
            "args": {
                "node_id": "P01",
                "label": "Will AI replace 50% of knowledge work by 2030?",
                "node_type": "prediction",
                "group_id": 11,
                "metadata": {"model_type": "Advanced", "status": "created"},
            },
        })
        assert result["inserted"] is True

        conn = brain.init_db()
        node = conn.execute("SELECT label, type FROM nodes WHERE id='P01'").fetchone()
        conn.close()
        assert node[0] == "Will AI replace 50% of knowledge work by 2030?"
        assert node[1] == "prediction"

    def test_folds_wire_to_graph(self, workspace):
        bridge.handle({
            "command": "add_node",
            "args": {"node_id": "P01", "label": "Test prediction", "node_type": "prediction", "group_id": 11},
        })
        bridge.handle({
            "command": "add_edge",
            "args": {"source_id": "P01", "target_id": "S01", "edge_type": "quantifies", "label": "prediction relevance"},
        })

        conn = brain.init_db()
        edge = conn.execute(
            "SELECT type FROM edges WHERE source_id='P01' AND target_id='S01'"
        ).fetchone()
        conn.close()
        assert edge[0] == "quantifies"
