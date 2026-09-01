"""Deterministic reader for dbt's own artifacts. NO LLM code belongs here
(see CLAUDE.md) -- this only parses manifest.json / catalog.json and a unified
diff, and returns plain structured data.

manifest.json is the authoritative source for lineage (which source() tables
and ref() models each model depends on) and, as a fallback, the
schema.yml-documented columns. catalog.json -- produced by `dbt docs
generate` against the built warehouse -- is the authoritative source for the
*complete* column list and the real column data types. Neither is ever
inferred from raw SQL.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from model_docs.models import ModelColumn, ModelStructure, ServeField

# Matches the model .sql paths in a unified diff -- both the `diff --git`
# header and the `+++ b/...` / `--- a/...` lines, on either path separator.
_DIFF_MODEL_PATH = re.compile(r"dbt_project[\\/]models[\\/](?P<rest>[\w\\/.-]+?\.sql)\b")


def load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _layer_from_path(relative_path: str) -> str:
    parts = re.split(r"[\\/]", relative_path.strip())
    return parts[0] if parts else ""


def _model_name_from_sql_path(sql_path: str) -> str:
    return re.split(r"[\\/]", sql_path)[-1][: -len(".sql")]


def changed_model_names_from_diff(diff_text: str) -> list[str]:
    """Model names whose .sql file appears in `diff_text`. schema.yml and
    any non-.sql path is ignored. Order-preserving, de-duplicated.

    Works identically on a real `git diff` and on a synthetic diff pasted
    into the workflow_dispatch input -- both are unified-diff text.
    """
    seen: list[str] = []
    for match in _DIFF_MODEL_PATH.finditer(diff_text):
        rest = match.group("rest")
        if rest.endswith("schema.yml"):
            continue
        name = _model_name_from_sql_path(rest)
        if name and name not in seen:
            seen.append(name)
    return seen


def _iter_model_nodes(manifest: dict):
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") == "model":
            yield uid, node


def _source_tables(node: dict) -> list[str]:
    """Fully-qualified source() tables, from the model's `sources` list
    (`[["mock_erp", "vbak"]]` -> `["mock_erp.vbak"]`)."""
    out: list[str] = []
    for entry in node.get("sources", []) or []:
        qualified = ".".join(entry)
        if qualified not in out:
            out.append(qualified)
    return out


def _referenced_models(node: dict) -> list[str]:
    out: list[str] = []
    for ref in node.get("refs", []) or []:
        # dbt >=1.5 refs are dicts ({"name": ...}); older ones are lists.
        name = ref.get("name") if isinstance(ref, dict) else (ref[0] if ref else None)
        if name and name not in out:
            out.append(name)
    return out


def _catalog_columns(catalog: dict, uid: str) -> list[ModelColumn] | None:
    node = catalog.get("nodes", {}).get(uid)
    if not node:
        return None
    cols = sorted(node.get("columns", {}).values(), key=lambda c: c.get("index", 0))
    return [ModelColumn(name=c["name"], data_type=c["type"], source="catalog") for c in cols]


def _manifest_columns(node: dict) -> list[ModelColumn]:
    out: list[ModelColumn] = []
    for name, meta in (node.get("columns") or {}).items():
        out.append(
            ModelColumn(name=name, data_type=(meta.get("data_type") or "unknown"), source="manifest")
        )
    return out


def _compiled_sql(manifest_path: Path, node: dict) -> str:
    code = node.get("compiled_code") or node.get("raw_code") or ""
    if code:
        return code
    compiled_path = node.get("compiled_path")
    if compiled_path:
        candidate = manifest_path.parent / compiled_path
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return ""


def build_model_structures(
    manifest_path: str | Path,
    catalog_path: str | Path | None = None,
) -> dict[str, ModelStructure]:
    """Every model in the project, keyed by model name. catalog_path is
    optional: without it, `columns` falls back to the schema.yml-documented
    subset and `columns_from_catalog` is False."""
    manifest_path = Path(manifest_path)
    manifest = load_json(manifest_path)
    catalog = load_json(catalog_path) if catalog_path and Path(catalog_path).exists() else None

    structures: dict[str, ModelStructure] = {}
    for uid, node in _iter_model_nodes(manifest):
        catalog_cols = _catalog_columns(catalog, uid) if catalog else None
        columns = catalog_cols if catalog_cols is not None else _manifest_columns(node)
        relative_path = node.get("path", "").replace("\\", "/")
        structures[node["name"]] = ModelStructure(
            name=node["name"],
            layer=_layer_from_path(relative_path),
            relative_path=relative_path,
            schema_name=node.get("schema", ""),
            source_tables=_source_tables(node),
            referenced_models=_referenced_models(node),
            columns=columns,
            compiled_sql=_compiled_sql(manifest_path, node),
            columns_from_catalog=catalog_cols is not None,
        )
    return structures


def serve_field_descriptions(manifest_path: str | Path) -> dict[tuple[str, str], str]:
    """(model_name, column_name) -> schema.yml description, for serve_ models
    that document their columns. Kept separate from build_model_structures so
    the catalog stays the single source for the column *list*."""
    manifest = load_json(manifest_path)
    out: dict[tuple[str, str], str] = {}
    for _uid, node in _iter_model_nodes(manifest):
        if not node["name"].startswith("serve_"):
            continue
        for col_name, meta in (node.get("columns") or {}).items():
            desc = (meta.get("description") or "").strip()
            if desc:
                out[(node["name"], col_name)] = desc
    return out


def serve_fields(
    structures: dict[str, ModelStructure],
    descriptions: dict[tuple[str, str], str] | None = None,
) -> list[ServeField]:
    """Every column of every serve_ model, flattened into data-dictionary
    rows. Column names are taken as-is -- the serve layer is rename-only, so
    they already are the business-friendly names."""
    descriptions = descriptions or {}
    rows: list[ServeField] = []
    for structure in sorted(structures.values(), key=lambda s: s.name):
        if not structure.name.startswith("serve_"):
            continue
        for column in structure.columns:
            rows.append(
                ServeField(
                    model=structure.name,
                    field=column.name,
                    data_type=column.data_type,
                    description=descriptions.get((structure.name, column.name)),
                )
            )
    return rows
