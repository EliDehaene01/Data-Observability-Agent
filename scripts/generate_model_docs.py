"""CI entry point for the per-model dbt documentation pipeline
(.github/workflows/on_dbt_change.yml). Runs alongside the code-change
validation, whenever a PR touches dbt_project/models/**.

For every changed model in landing/ prep/ serve/ it publishes a Confluence
child page under a "Model Documentation" parent (space CONFLUENCE_SPACE_KEY)
containing:
  - source tables + ref() lineage + column names/types  -- DETERMINISTIC,
    straight from dbt's manifest.json + catalog.json (never LLM-inferred)
  - a plain-English business-logic summary               -- the one LLM call
    here, agent/nodes/summarize_model_logic.py (separate from
    classify_discrepancy; see CLAUDE.md)

It also regenerates a single "Serve Layer Overview" data-dictionary page
listing every field across all serve_ models -- deterministic, from the
serve models' own column names (rename-only layer, so those already are the
business-friendly names).

Inputs (all from the environment, set by the workflow):
  GITHUB_EVENT_NAME              "pull_request" or "workflow_dispatch"
  SYNTHETIC_SQL_DIFF             the manual synthetic diff (workflow_dispatch only)
  PR_BASE_SHA / PR_HEAD_SHA      diff endpoints (pull_request only)
  MANIFEST_PATH / CATALOG_PATH   dbt artifacts (default dbt_project/target/*.json)
  ANTHROPIC_API_KEY, CONFLUENCE_* used by the LLM node and the docs connector
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

from agent.nodes.summarize_model_logic import summarize_model_logic  # noqa: E402
from connectors.docs.confluence import ConfluenceDocsConnector  # noqa: E402
from model_docs.manifest import (  # noqa: E402
    build_model_structures,
    changed_model_names_from_diff,
    serve_field_descriptions,
    serve_fields,
)
from model_docs.render import (  # noqa: E402
    SERVE_OVERVIEW_TITLE,
    model_page_title,
    render_model_page,
    render_serve_overview_page,
)

MODEL_DOCS_PARENT_TITLE = "Model Documentation"
DOCUMENTED_LAYERS = {"landing", "prep", "serve"}
DEFAULT_MANIFEST_PATH = "dbt_project/target/manifest.json"
DEFAULT_CATALOG_PATH = "dbt_project/target/catalog.json"

logger = logging.getLogger("generate_model_docs")


def _models_sql_diff() -> str:
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return os.environ.get("SYNTHETIC_SQL_DIFF", "")
    base, head = os.environ.get("PR_BASE_SHA"), os.environ.get("PR_HEAD_SHA")
    if not (base and head):
        logger.warning("no PR_BASE_SHA/PR_HEAD_SHA and not workflow_dispatch -- no diff to inspect")
        return ""
    result = subprocess.run(
        ["git", "diff", base, head, "--", "dbt_project/models"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def main() -> int:
    manifest_path = os.environ.get("MANIFEST_PATH", DEFAULT_MANIFEST_PATH)
    catalog_path = os.environ.get("CATALOG_PATH", DEFAULT_CATALOG_PATH)

    if not Path(manifest_path).exists():
        print(f"manifest not found at {manifest_path} -- run `dbt run` / `dbt docs generate` first")
        return 1

    diff_text = _models_sql_diff()
    changed = changed_model_names_from_diff(diff_text)
    print(f"Changed models in diff: {changed or '(none)'}")

    structures = build_model_structures(manifest_path, catalog_path)
    descriptions = serve_field_descriptions(manifest_path)
    connector = ConfluenceDocsConnector()

    documented: list[str] = []
    for name in changed:
        structure = structures.get(name)
        if structure is None:
            print(f"  - {name}: not a model in the manifest, skipping")
            continue
        if structure.layer not in DOCUMENTED_LAYERS:
            print(f"  - {name}: layer {structure.layer!r} not documented, skipping")
            continue

        summary = summarize_model_logic(name, structure.compiled_sql, structure.layer)
        url = connector.publish_page(
            title=model_page_title(name),
            content=render_model_page(structure, summary),
            parent_title=MODEL_DOCS_PARENT_TITLE,
            content_format="storage",
        )
        documented.append(name)
        print(f"  - {name}: published -> {url}")

    # The Serve Layer Overview is cheap and always regenerated when any model
    # changed, so it never drifts from the serve models' actual columns.
    if changed:
        fields = serve_fields(structures, descriptions)
        url = connector.publish_page(
            title=SERVE_OVERVIEW_TITLE,
            content=render_serve_overview_page(fields),
            parent_title=MODEL_DOCS_PARENT_TITLE,
            content_format="storage",
        )
        print(f"  - {SERVE_OVERVIEW_TITLE}: {len(fields)} fields -> {url}")

    print(f"Documented {len(documented)} model page(s): {documented or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
