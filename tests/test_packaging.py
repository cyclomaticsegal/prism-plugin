"""Packaging tests.

The UTF-8 guard in scripts/package.sh exists specifically to prevent the v1
private-content leak class — where a `.skill` ZIP archive was renamed to
SKILL.md without unzipping and shipped as a binary blob containing the
plug-in author's private thesis. Without a regression test, the guard is
silent insurance that nobody verifies.
"""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _assert_package_script_exists():
    script = PROJECT_ROOT / "scripts" / "package.sh"
    assert script.exists(), "scripts/package.sh missing — packaging guard test cannot run"


class TestUTF8Guard:
    """Run package.sh against a doctored copy of the repo and assert it exits non-zero."""

    @pytest.fixture
    def fake_repo(self, tmp_path):
        """Stand up a minimal repo skeleton that package.sh will accept."""
        _assert_package_script_exists()
        root = tmp_path / "fake-repo"
        root.mkdir()

        (root / "scripts").mkdir()
        shutil.copy2(str(PROJECT_ROOT / "scripts" / "package.sh"), str(root / "scripts" / "package.sh"))

        # Minimal .claude-plugin/plugin.json so package.sh can read the version.
        (root / ".claude-plugin").mkdir()
        (root / ".claude-plugin" / "plugin.json").write_text(
            '{"name":"prism","version":"0.0.1-test"}'
        )
        (root / ".claude-plugin" / "marketplace.json").write_text(
            '{"name":"cyclomaticsegal","owner":{"name":"Test"},"plugins":[]}'
        )
        (root / ".mcp.json").write_text('{"mcpServers":{}}')
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text('{"hooks":{}}')
        (root / "scripts").mkdir(exist_ok=True)
        (root / "scripts" / "install-deps.sh").write_text("#!/bin/bash\n")
        (root / "LICENSE").write_text("MIT (test)")
        (root / "README.md").write_text("# test")

        # Engine + server stubs.
        (root / "engine").mkdir()
        (root / "engine" / "brain.py").write_text("# stub")
        (root / "engine" / "bridge.py").write_text("# stub")
        (root / "engine" / "bootstrap.sh").write_text("#!/bin/bash\n")
        (root / "engine" / "requirements.txt").write_text("")

        (root / "server").mkdir()
        (root / "server" / "index.js").write_text("// stub")
        (root / "server" / "package.json").write_text(
            '{"name":"prism-test","type":"module","version":"0.0.1-test","dependencies":{}}'
        )
        (root / "server" / "package-lock.json").write_text(
            '{"name":"prism-test","version":"0.0.1-test","lockfileVersion":3,"requires":true,"packages":{"":{"name":"prism-test","version":"0.0.1-test"}}}'
        )
        # Pre-create node_modules so `npm install --production` is a no-op.
        (root / "server" / "node_modules").mkdir()

        # Templates + skills + reference-brain + extensions skeletons.
        (root / "templates").mkdir()
        (root / "templates" / "graph-explorer-template.html").write_text("<html></html>")
        (root / "skills").mkdir()
        (root / "reference-brain").mkdir()
        (root / "reference-brain" / "domains-reference.json").write_text("[]")
        (root / "reference-brain" / "AXIOMS-reference.md").write_text("# axioms")
        (root / "reference-brain" / "build.py").write_text("# stub")
        (root / "reference-brain" / "sources").mkdir()
        (root / "reference-brain" / "sources" / "S01-stub.md").write_text("stub")
        (root / "extensions").mkdir()

        return root

    def _run_package(self, root):
        """Run scripts/package.sh inside the fake repo, return CompletedProcess."""
        return subprocess.run(
            ["bash", "scripts/package.sh"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_clean_repo_packages_successfully(self, fake_repo):
        """Sanity: a repo with no SKILL.md files at all packages cleanly."""
        result = self._run_package(fake_repo)
        assert result.returncode == 0, (
            f"Clean fake repo failed to package: stderr={result.stderr}"
        )

    def test_utf8_skill_packages_successfully(self, fake_repo):
        """A normal markdown SKILL.md packages cleanly."""
        skill_dir = fake_repo / "skills" / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Test skill\n\nNormal markdown content.\n")

        result = self._run_package(fake_repo)
        assert result.returncode == 0, (
            f"UTF-8 SKILL.md should package cleanly: stderr={result.stderr}"
        )

    def test_zip_archive_disguised_as_skill_md_is_rejected(self, fake_repo):
        """The exact v1 leak shape: a .skill ZIP renamed to SKILL.md fails the guard."""
        skill_dir = fake_repo / "skills" / "broken-skill"
        skill_dir.mkdir()
        skill_path = skill_dir / "SKILL.md"

        # Build a real ZIP archive at the SKILL.md path.
        with zipfile.ZipFile(str(skill_path), "w") as zf:
            zf.writestr("inner-content.txt", "this would have been private content")

        result = self._run_package(fake_repo)
        assert result.returncode != 0, (
            f"Packaging should fail when SKILL.md is a ZIP archive. "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        # The error message should clearly identify what went wrong.
        combined = (result.stdout + result.stderr).lower()
        assert "utf-8" in combined or "expected utf" in combined, (
            f"Error message should mention UTF-8: {result.stderr}"
        )

    def test_non_utf8_binary_skill_md_is_rejected(self, fake_repo):
        """Any non-UTF-8 binary content at SKILL.md (not just ZIPs) is rejected."""
        skill_dir = fake_repo / "skills" / "binary-skill"
        skill_dir.mkdir()
        # Random bytes that aren't valid UTF-8.
        (skill_dir / "SKILL.md").write_bytes(b"\xff\xfe\x00\x00\xde\xad\xbe\xef" * 32)

        result = self._run_package(fake_repo)
        assert result.returncode != 0, (
            f"Packaging should fail for binary SKILL.md. stderr={result.stderr}"
        )
