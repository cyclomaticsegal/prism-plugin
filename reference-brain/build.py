#!/usr/bin/env python3
"""Build the reference brain from public-domain source files.

Run from the repo root:
    python3 reference-brain/build.py

Creates brain-reference.db and GRAPH-reference.json in reference-brain/.

This script seeds the `domains` and `axioms` tables in prism-brain.db directly
via `upsert_domain` and `revise_axiom`. Inside the temporary `_build/`
workspace, files land in `_build/prism/` per the standard PRISM layout —
the reference brain and a real user workspace are structurally identical.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
ENGINE_DIR = REPO_ROOT / "engine"

sys.path.insert(0, str(ENGINE_DIR))
import brain

SOURCES_DIR = SCRIPT_DIR / "sources"
DOMAINS_FILE = SCRIPT_DIR / "domains-reference.json"
OUTPUT_DB = SCRIPT_DIR / "brain-reference.db"
OUTPUT_GRAPH = SCRIPT_DIR / "GRAPH-reference.json"


def _seed_domains_from_reference():
    """Load domains-reference.json and write each row into the `domains` table."""
    rows = json.loads(DOMAINS_FILE.read_text())
    print("=== Seeding domains table ===")
    for d in rows:
        result = brain.upsert_domain(
            label=d["label"],
            keywords=d["keywords"],
            short_label=d.get("short_label"),
            color=d.get("color"),
        )
        print(f"  D{result['id']}: {result['label']}")


def _write_reference_axioms():
    """Author the reference axioms via revise_axiom().

    These mirror the historic AXIOMS-reference.md content but as structured
    table rows with citations and boundaries — the form the plug-in produces
    via prism_core_axioms_revise during normal use.
    """
    print("\n=== Writing reference axioms ===")

    axioms = [
        {
            "key": "division-of-knowledge-parallels-labour",
            "body": (
                "Just as Smith showed that dividing physical labour creates "
                "exponential productivity gains, the division of intellectual "
                "labour — specialisation of knowledge domains — drives "
                "progress. But it also creates fragmentation. The thinkers "
                "who bridge specialisations (Bacon, Franklin, Mill) produce "
                "disproportionate insight."
            ),
            "citations": ["S01", "S18", "S26"],
            "boundary": (
                "Breaks down when specialisation produces incommensurable "
                "paradigms — when experts in different domains literally "
                "cannot understand each other's frameworks (Kuhn-style "
                "incommensurability)."
            ),
        },
        {
            "key": "legitimate-power-requires-institutional-constraints",
            "body": (
                "Every political theorist in this corpus — Locke, Madison, "
                "Machiavelli, Tocqueville — grapples with the same problem: "
                "how to prevent power from becoming tyrannical. The answer "
                "is always institutional design, never individual virtue."
            ),
            "citations": ["S11", "S13", "S14", "S15"],
            "boundary": (
                "Fails in contexts where institutions themselves are captured, "
                "or where the population lacks the civic infrastructure "
                "Tocqueville describes. Machiavelli's realism suggests "
                "institutions are only as strong as the power dynamics "
                "sustaining them."
            ),
        },
        {
            "key": "moral-status-of-productivity",
            "body": (
                "From Smith through Carnegie there is a persistent assumption "
                "that productive activity is morally good — that wealth "
                "creation benefits society. This assumption is challenged by "
                "Marx (exploitation), Thoreau (simplicity), and Douglass "
                "(whose labour was stolen, not exchanged)."
            ),
            "citations": ["S01", "S04", "S21", "S23", "S27"],
            "boundary": (
                "Assumes a context of voluntary exchange. When labour is "
                "coerced (Douglass) or productivity destroys non-market "
                "goods (Thoreau), the moral calculus inverts."
            ),
        },
        {
            "key": "empiricism-vs-rationalism-as-operating-systems",
            "body": (
                "The Descartes-Hume-Kant sequence is not just a philosophical "
                "debate; it is a template for how any field resolves the "
                "tension between top-down theory and bottom-up evidence. "
                "Science (Bacon, Newton, Darwin) chose empiricism. Ethics "
                "(Kant, Mill) remains split."
            ),
            "citations": ["S08", "S09", "S10", "S16", "S18"],
            "boundary": (
                "Kant's synthesis (experience provides content, reason "
                "provides structure) is elegant but does not resolve "
                "practical disagreements about which framework to apply in "
                "specific cases."
            ),
        },
        {
            "key": "bridge-labour-knowledge-power",
            "body": (
                "Smith's division of labour, Bacon's scientific method, and "
                "Madison's institutional design all address the same problem: "
                "how to organise collective human effort to produce outcomes "
                "no individual could achieve alone. The economic, epistemic, "
                "and political solutions are structurally parallel."
            ),
            "citations": ["S01", "S13", "S18"],
            "boundary": (
                "The parallel breaks where the problem-space is "
                "non-decomposable — when collective effort cannot be "
                "factored into independent sub-tasks (mass coordination "
                "problems, cultural change)."
            ),
        },
        {
            "key": "bridge-individual-vs-collective",
            "body": (
                "Emerson's self-reliance, Mill's utilitarianism, and Smith's "
                "invisible hand represent three different answers to: should "
                "the individual optimise for themselves or for the group? "
                "Emerson says self; Mill says group; Smith says self-interest "
                "accidentally serves the group."
            ),
            "citations": ["S01", "S24", "S29"],
            "boundary": (
                "The 'self-interest serves the group' claim assumes "
                "well-functioning markets and bounded externalities — both "
                "of which can fail."
            ),
        },
        {
            "key": "bridge-evidence-and-authority",
            "body": (
                "Galileo's confrontation with Church authority, Bacon's "
                "rejection of received wisdom, and Paine's rejection of "
                "monarchical authority all challenge the same structure: "
                "argument from authority. The scientific revolution and the "
                "democratic revolution share an epistemic root."
            ),
            "citations": ["S12", "S18", "S19"],
            "boundary": (
                "The rejection of authority is itself an authority claim. "
                "Pure scepticism collapses into nihilism without some "
                "ground for trust."
            ),
        },
    ]

    for a in axioms:
        result = brain.revise_axiom(
            key=a["key"],
            body=a["body"],
            citations=a["citations"],
            boundary=a["boundary"],
        )
        print(f"  axiom #{result['id']}: {result['key']}")


def build():
    print("=== Building PRISM Reference Brain ===")

    workspace = SCRIPT_DIR / "_build"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir()

    brain.configure(workspace_root=str(workspace), session_dir=None)
    brain.reset_embedder()
    brain.reset_domains_cache()
    brain.EMBEDDING_BACKEND = "tfidf"

    # Seed sources into the namespaced location the engine expects.
    brain.SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for f in SOURCES_DIR.iterdir():
        if f.is_file():
            shutil.copy2(str(f), str(brain.SOURCES_DIR / f.name))

    # Initialise the database (schema includes domains, axioms, predictions).
    conn = brain.init_db()
    conn.close()

    # Seed domains table from the reference configuration BEFORE classification.
    _seed_domains_from_reference()
    brain.reset_domains_cache()

    # Ingest source files.
    conn = brain.init_db()
    print("\n=== Ingesting reference sources ===")
    brain.ingest_documents(conn)

    # Auto-classify and wire domain edges.
    print("\n=== Domain Classification ===")
    files = brain.find_source_files()
    for path in files:
        sid = brain.file_to_source_id(path)
        try:
            classified = brain.classify_source_domains(sid, conn)
            domain_nums = [c["domain_id"] for c in classified]
            if domain_nums:
                brain.wire_source_to_domains(sid, domain_nums, conn)
                print(f"  {sid}: domains {domain_nums}")
        except Exception as e:
            print(f"  {sid}: classification failed ({e})")

    # Create concept nodes for key ideas (mirrors the curated reference graph).
    print("\n=== Creating Concept Nodes ===")
    concepts = _reference_concepts()
    for c in concepts:
        brain.propose_concept(
            concept_id=c["id"], label=c["label"],
            domain_id=c["domain"], source_id=c["source"],
            edges=c.get("edges", []), conn=conn,
        )
        brain.accept_proposal(c["id"], conn)
        print(f"  {c['id']}: {c['label']}")

    brain._save_stats_snapshot(conn)
    n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_domains = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
    n_axioms = conn.execute("SELECT COUNT(*) FROM axioms WHERE superseded_by IS NULL").fetchone()[0]
    print(
        f"\n  Nodes: {n_nodes} | Edges: {n_edges} | Chunks: {n_chunks} | "
        f"Domains: {n_domains} | Axioms: {n_axioms}"
    )
    conn.close()

    # Author the reference axioms.
    _write_reference_axioms()

    # Export artefacts.
    shutil.copy2(str(brain.DB_EXPORT_PATH), str(OUTPUT_DB))
    print(f"\n  Exported: {OUTPUT_DB}")

    brain.export_graph_json()
    if brain.GRAPH_JSON.exists():
        shutil.copy2(str(brain.GRAPH_JSON), str(OUTPUT_GRAPH))
        print(f"  Exported: {OUTPUT_GRAPH}")

    # prism-axioms.md is regenerated as a projection inside the workspace —
    # users will see this same projection in their own workspaces after the
    # first axiom revision. Reference snapshot is the markdown checked in
    # at reference-brain/AXIOMS-reference.md.

    shutil.rmtree(workspace)
    print("\n=== Reference Brain Complete ===")


def _reference_concepts():
    """Pre-defined concepts for the reference brain."""
    return [
        {"id": "C01", "label": "Division of Labour", "domain": 1, "source": "S01",
         "edges": []},
        {"id": "C02", "label": "Labour Theory of Value", "domain": 1, "source": "S02",
         "edges": [{"target": "C01", "type": "relates_to", "label": "production theory"}]},
        {"id": "C03", "label": "Commodity Fetishism", "domain": 1, "source": "S04",
         "edges": [{"target": "C02", "type": "critiques", "label": "challenges value theory"}]},
        {"id": "C04", "label": "Malthusian Population Trap", "domain": 1, "source": "S05",
         "edges": [{"target": "C01", "type": "relates_to", "label": "limits on growth"}]},
        {"id": "C05", "label": "Allegory of the Cave", "domain": 2, "source": "S06",
         "edges": []},
        {"id": "C06", "label": "Eudaimonia", "domain": 7, "source": "S07",
         "edges": [{"target": "C05", "type": "relates_to", "label": "the good life"}]},
        {"id": "C07", "label": "Cogito Ergo Sum", "domain": 2, "source": "S08",
         "edges": [{"target": "C05", "type": "critiques", "label": "certainty vs perception"}]},
        {"id": "C08", "label": "Problem of Induction", "domain": 2, "source": "S09",
         "edges": [{"target": "C07", "type": "critiques", "label": "challenges rational certainty"}]},
        {"id": "C09", "label": "Synthetic A Priori", "domain": 2, "source": "S10",
         "edges": [{"target": "C08", "type": "explains", "label": "resolves empiricism-rationalism"},
                   {"target": "C07", "type": "explains", "label": "grounds for knowledge"}]},
        {"id": "C10", "label": "Natural Right to Property", "domain": 3, "source": "S11",
         "edges": [{"target": "C01", "type": "enables", "label": "property enables trade"}]},
        {"id": "C11", "label": "Tyranny of the Majority", "domain": 3, "source": "S14",
         "edges": []},
        {"id": "C12", "label": "Faction Theory", "domain": 3, "source": "S13",
         "edges": [{"target": "C11", "type": "relates_to", "label": "democratic risk"},
                   {"target": "C10", "type": "relates_to", "label": "property and factions"}]},
        {"id": "C13", "label": "Political Realism", "domain": 3, "source": "S15",
         "edges": []},
        {"id": "C14", "label": "Natural Selection", "domain": 4, "source": "S16",
         "edges": []},
        {"id": "C15", "label": "Idols of the Mind", "domain": 4, "source": "S18",
         "edges": [{"target": "C08", "type": "relates_to", "label": "obstacles to knowledge"},
                   {"target": "C05", "type": "relates_to", "label": "perception vs reality"}]},
        {"id": "C16", "label": "Heliocentrism", "domain": 4, "source": "S19",
         "edges": [{"target": "C15", "type": "exemplifies", "label": "overcoming idols"}]},
        {"id": "C17", "label": "Circulation of Blood", "domain": 4, "source": "S20",
         "edges": [{"target": "C14", "type": "relates_to", "label": "biological mechanism"}]},
        {"id": "C18", "label": "Literacy as Liberation", "domain": 5, "source": "S21",
         "edges": [{"target": "C10", "type": "critiques", "label": "property in persons"}]},
        {"id": "C19", "label": "Women's Rational Education", "domain": 5, "source": "S22",
         "edges": [{"target": "C18", "type": "relates_to", "label": "education as freedom"}]},
        {"id": "C20", "label": "Economy of Simplicity", "domain": 5, "source": "S23",
         "edges": [{"target": "C01", "type": "critiques", "label": "challenges productivity worship"}]},
        {"id": "C21", "label": "Self-Reliance", "domain": 5, "source": "S24",
         "edges": [{"target": "C06", "type": "relates_to", "label": "individual excellence"}]},
        {"id": "C22", "label": "Systematic Innovation", "domain": 6, "source": "S25",
         "edges": [{"target": "C15", "type": "enables", "label": "method enables discovery"}]},
        {"id": "C23", "label": "Gospel of Wealth", "domain": 6, "source": "S27",
         "edges": [{"target": "C01", "type": "relates_to", "label": "wealth from industry"},
                   {"target": "C10", "type": "relates_to", "label": "property and responsibility"}]},
        {"id": "C24", "label": "Scientific Management", "domain": 6, "source": "S28",
         "edges": [{"target": "C01", "type": "extends", "label": "systematic division of labour"}]},
        {"id": "C25", "label": "Greatest Happiness Principle", "domain": 7, "source": "S29",
         "edges": [{"target": "C06", "type": "critiques", "label": "utility vs virtue"}]},
        {"id": "C26", "label": "Categorical Imperative", "domain": 7, "source": "S30",
         "edges": [{"target": "C25", "type": "critiques", "label": "duty vs consequences"},
                   {"target": "C09", "type": "relates_to", "label": "moral a priori"}]},
    ]


if __name__ == "__main__":
    build()
