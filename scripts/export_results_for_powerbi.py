"""Export results_store data to CSV for the local Power BI companion
(powerbi/DataObservabilityAgent.pbip). Like scripts/generate_dashboard.py
this reads only from results_store -- no reconciliation/agent logic here
(CLAUDE.md dashboard boundary rule).

results_store/results.duckdb is no longer tracked on main -- both trigger
workflows commit it to the dedicated data-results branch instead (see
README.md / docs/architecture.md §8), since main now requires PRs + a
review. So this script always pulls the latest copy from
origin/data-results via git rather than reading whatever (stale, usually
absent) copy sits in the local main checkout.

Writes two files into powerbi/data/ (gitignored -- regenerated data):

  reconciliation_results.csv   one row per ReconciliationResult (the flat
                               results table, all columns)
  classification_history.csv   one row per trigger_type="code_change" run
                               (run metadata + the four classification
                               columns + flagged/total check counts)

Usage: python scripts/export_results_for_powerbi.py [output_dir]
"""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from results_store.reader import get_all_results

DEFAULT_OUTPUT_DIR = REPO_ROOT / "powerbi" / "data"


def _fetch_results_db_from_data_results() -> Path:
    """Pull the current results_store/results.duckdb out of the
    data-results branch into a local temp file and return its path, without
    touching the caller's working tree/branch."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "data-results"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            ["git", "show", "origin/data-results:results_store/results.duckdb"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        raise SystemExit(
            "Could not read results_store/results.duckdb from origin/data-results "
            "-- has on_data_load.yml or on_dbt_change.yml run at least once yet? "
            f"({stderr.strip()})"
        ) from exc

    tmp = tempfile.NamedTemporaryFile(prefix="results-", suffix=".duckdb", delete=False)
    tmp.write(result.stdout)
    tmp.close()
    return Path(tmp.name)


_RESULT_COLUMNS = [
    "run_id",
    "environment",
    "trigger_type",
    "run_timestamp",
    "check_type",
    "table",
    "metric",
    "source_value",
    "target_value",
    "diff_pct",
    "threshold",
    "status",
    "final_classification",
    "confidence",
    "pr_claims_no_impact",
    "downgraded",
]

_CLASSIFICATION_COLUMNS = [
    "run_id",
    "environment",
    "run_timestamp",
    "final_classification",
    "confidence",
    "pr_claims_no_impact",
    "downgraded",
    "total_checks",
    "flagged_checks",
]


def _classification_history(rows: list[dict]) -> list[dict]:
    """Collapse the flat rows to one summary per code_change run_id,
    newest-first (get_all_results already returns newest-first)."""
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if row["trigger_type"] != "code_change":
            continue
        if row["run_id"] not in grouped:
            grouped[row["run_id"]] = []
            order.append(row["run_id"])
        grouped[row["run_id"]].append(row)

    history = []
    for run_id in order:
        run_rows = grouped[run_id]
        first = run_rows[0]
        history.append(
            {
                "run_id": run_id,
                "environment": first["environment"],
                "run_timestamp": first["run_timestamp"],
                "final_classification": first.get("final_classification"),
                "confidence": first.get("confidence"),
                "pr_claims_no_impact": first.get("pr_claims_no_impact"),
                "downgraded": first.get("downgraded"),
                "total_checks": len(run_rows),
                "flagged_checks": sum(1 for r in run_rows if r["status"] == "flag"),
            }
        )
    return history


def _clean(value: object) -> object:
    """Normalise for Power BI's Csv.Document type coercion: timestamps to
    second-precision ISO (`yyyy-MM-dd HH:mm:ss`), booleans to lowercase
    `true`/`false` (what M's Logical.FromText expects), None to ''."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: _clean(v) for k, v in row.items()} for row in rows)


def main() -> None:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = _fetch_results_db_from_data_results()
    try:
        rows = get_all_results(db_path=db_path)
    finally:
        db_path.unlink(missing_ok=True)
    history = _classification_history(rows)

    results_path = output_dir / "reconciliation_results.csv"
    history_path = output_dir / "classification_history.csv"
    _write_csv(results_path, _RESULT_COLUMNS, rows)
    _write_csv(history_path, _CLASSIFICATION_COLUMNS, history)

    print(f"Wrote {len(rows)} rows to {results_path}")
    print(f"Wrote {len(history)} runs to {history_path}")


if __name__ == "__main__":
    main()
