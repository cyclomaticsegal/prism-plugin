#!/usr/bin/env bash
# Package PRISM as a distributable archive.
#
# Creates dist/prism-<version>.tar.gz containing everything needed to install
# the plugin. Excludes tests, .git, __pycache__, build artifacts, and docs.
#
# Usage:
#   bash scripts/package.sh
#
# The Cowork .plugin format is expected to be a bundled archive. This script
# produces the archive; the format may need adjustment once the Cowork plugin
# spec is finalised.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VERSION=$(python3 -c "import json; print(json.load(open('$REPO_ROOT/.claude-plugin/plugin.json'))['version'])")
DIST_DIR="$REPO_ROOT/dist"
PACKAGE_NAME="prism-${VERSION}"
STAGING="$DIST_DIR/$PACKAGE_NAME"

echo "=== Packaging PRISM v${VERSION} ==="

# Clean previous builds
rm -rf "$DIST_DIR"
mkdir -p "$STAGING"

# Copy plugin files
mkdir -p "$STAGING/.claude-plugin"
cp "$REPO_ROOT/.claude-plugin/plugin.json" "$STAGING/.claude-plugin/"
cp "$REPO_ROOT/.claude-plugin/marketplace.json" "$STAGING/.claude-plugin/" 2>/dev/null || true
cp "$REPO_ROOT/.mcp.json" "$STAGING/"
cp -r "$REPO_ROOT/hooks" "$STAGING/hooks"
cp -r "$REPO_ROOT/scripts" "$STAGING/scripts"
cp "$REPO_ROOT/LICENSE" "$STAGING/"
cp "$REPO_ROOT/README.md" "$STAGING/"

# Engine
mkdir -p "$STAGING/engine"
cp "$REPO_ROOT/engine/brain.py" "$STAGING/engine/"
cp "$REPO_ROOT/engine/bridge.py" "$STAGING/engine/"
cp "$REPO_ROOT/engine/bootstrap.sh" "$STAGING/engine/"
cp "$REPO_ROOT/engine/requirements.txt" "$STAGING/engine/"

# Server
mkdir -p "$STAGING/server"
cp "$REPO_ROOT/server/index.js" "$STAGING/server/"
cp "$REPO_ROOT/server/package.json" "$STAGING/server/"
cp "$REPO_ROOT/server/package-lock.json" "$STAGING/server/"

# Templates
cp -r "$REPO_ROOT/templates" "$STAGING/templates"

# Skills
cp -r "$REPO_ROOT/skills" "$STAGING/skills"

# Guard: every SKILL.md must be UTF-8 text. Catches the v1.0.0 bug where a
# .skill ZIP archive was renamed to SKILL.md without unzipping and shipped
# as a binary blob containing private content.
echo "  Verifying SKILL.md files are UTF-8 text..."
while IFS= read -r -d '' skill_file; do
    encoding=$(file --mime-encoding -b "$skill_file")
    if [[ "$encoding" != "utf-8" && "$encoding" != "us-ascii" ]]; then
        echo "  ERROR: $skill_file is $encoding (expected utf-8). Was a corrupt zip blob in v1.0.0." >&2
        exit 1
    fi
done < <(find "$STAGING/skills" -name "SKILL.md" -print0)

# Reference brain (sources + config, not built DB)
mkdir -p "$STAGING/reference-brain/sources"
cp "$REPO_ROOT/reference-brain/domains-reference.json" "$STAGING/reference-brain/"
cp "$REPO_ROOT/reference-brain/AXIOMS-reference.md" "$STAGING/reference-brain/"
cp "$REPO_ROOT/reference-brain/build.py" "$STAGING/reference-brain/"
cp "$REPO_ROOT"/reference-brain/sources/*.md "$STAGING/reference-brain/sources/"

# Extensions (reference implementations)
cp -r "$REPO_ROOT/extensions" "$STAGING/extensions"

# Install server dependencies
echo "  Installing server dependencies..."
cd "$STAGING/server" && npm install --production --silent 2>&1 | tail -1
cd "$REPO_ROOT"

# Create archive
echo "  Creating archive..."
cd "$DIST_DIR"
tar -czf "${PACKAGE_NAME}.tar.gz" "$PACKAGE_NAME"

# Report
ARCHIVE="$DIST_DIR/${PACKAGE_NAME}.tar.gz"
SIZE=$(du -h "$ARCHIVE" | cut -f1)
FILE_COUNT=$(tar -tzf "$ARCHIVE" | wc -l | tr -d ' ')
echo ""
echo "=== Package Complete ==="
echo "  Archive: $ARCHIVE"
echo "  Size: $SIZE"
echo "  Files: $FILE_COUNT"
echo ""
echo "To install: extract and add the directory as a marketplace:"
echo "  /plugin marketplace add /path/to/${PACKAGE_NAME}"
echo "  /plugin install prism@cyclomaticsegal"
