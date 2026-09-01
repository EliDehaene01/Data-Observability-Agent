"""Tests for scripts/export_results_for_powerbi.py -- the two CSV-shaping
helpers only. No database: synthetic rows in the shape
results_store.reader.get_all_results() returns (newest-first).
"""

from __future__ import annotations

import csv
from datetime import datetime

from scripts.export_results_for_powerbi import (
    _classification_history,
    _clean,
    _write_csv,
)


def _row(**kw):
    base = {
        "run_id": "r1",
        "environment": "dev",
        "trigger_type": "code_change",
        "run_timestamp": datetime(2026, 9, 1, 16, 0, 0),
        "status": "pass",
        "final_classification": "expected",
        "confidence": 0.8,
        "pr_claims_no_impact": False,
        "downgraded": False,
    }
    base.update(kw)
    return base


def test_classification_history_groups_per_code_change_run():
    rows = [
        _row(run_id="r2", status="flag"),
        _row(run_id="r2", status="pass"),
        _row(run_id="r1", trigger_type="data_load"),  # excluded
        _row(run_id="r3", status="flag", downgraded=True, pr_claims_no_impact=True),
    ]
    hist = _classification_history(rows)

    assert [h["run_id"] for h in hist] == ["r2", "r3"]  # order preserved, data_load dropped
    assert hist[0]["total_checks"] == 2
    assert hist[0]["flagged_checks"] == 1
    assert hist[1]["downgraded"] is True


def test_clean_formats_for_power_query():
    assert _clean(None) == ""
    assert _clean(True) == "true"
    assert _clean(False) == "false"
    assert _clean(datetime(2026, 9, 1, 16, 5, 9)) == "2026-09-01 16:05:09"
    assert _clean(0.55) == 0.55


def test_write_csv_applies_clean(tmp_path):
    path = tmp_path / "out.csv"
    _write_csv(path, ["run_id", "downgraded", "confidence"], [_row(run_id="r9")])
    line = list(csv.DictReader(path.open()))[0]
    assert line == {"run_id": "r9", "downgraded": "false", "confidence": "0.8"}
