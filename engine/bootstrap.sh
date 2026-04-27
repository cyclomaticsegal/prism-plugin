#!/usr/bin/env bash
# bootstrap.sh — Install Python dependencies for brain.py
# Run this at the start of each Cowork session (VM resets wipe packages).
#
# Usage:
#   bash /path/to/prism-plugin/engine/bootstrap.sh
#
# What it installs:
#   - numpy, scikit-learn       (TF-IDF embeddings — the always-works fallback)
#   - sentence-transformers     (primary embedder — model download may fail behind proxy)
#   - requests, beautifulsoup4,
#     readability-lxml          (optional — better text extraction for prism_core_ingest_url)
#
# After running, brain.py is ready for: ingest, search, graph, stats, ingest_url, ingest_text.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== PRISM Bootstrap ==="
echo "Installing Python dependencies..."

# Core deps (always work on the VM)
pip3 install --break-system-packages --quiet numpy scikit-learn 2>&1 | tail -1

# sentence-transformers (model download may fail behind proxy — brain.py
# auto-falls back to TF-IDF, which is fine)
pip3 install --break-system-packages --quiet sentence-transformers 2>&1 | tail -1

# URL extraction (optional — prism_core_ingest_url uses these for better extraction;
# falls back to regex stripping if any are missing)
pip3 install --break-system-packages --quiet requests beautifulsoup4 readability-lxml 2>&1 | tail -1 || true

# Verify imports work
python3 -c "
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import normalize
import numpy as np
print('OK: scikit-learn + numpy ready')

try:
    from sentence_transformers import SentenceTransformer
    print('OK: sentence-transformers available (model download may still need proxy)')
except ImportError:
    print('WARN: sentence-transformers not available — TF-IDF fallback will be used')

try:
    import requests, bs4
    try:
        import readability
        print('OK: URL extraction stack ready (requests + bs4 + readability)')
    except ImportError:
        print('OK: URL extraction partial (requests + bs4; readability missing — bs4 fallback used)')
except ImportError:
    print('WARN: requests/bs4 missing — prism_core_ingest_url falls back to regex stripping')
"

# Quick sanity: can brain.py at least parse?
python3 -c "import importlib.util; spec = importlib.util.spec_from_file_location('brain', '${SCRIPT_DIR}/brain.py'); print('OK: brain.py loadable')"

echo "=== Bootstrap complete ==="
echo "Ready: python3 ${SCRIPT_DIR}/brain.py [ingest|search|graph|stats]"
