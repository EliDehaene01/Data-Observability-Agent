"""Tests for connectors/reporting/html_dashboard.py's run drill-down --
against a temp results store written through results_store.writer, so no
Postgres/DuckDB warehouse is needed."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from connectors.reporting.html_dashboard import HtmlDashboardConnector
from reconciliation.models import ReconciliationResult, ReconciliationRun
from results_store.reader import get_run_by_id
from results_store.writer import write_run


def _result(table, metric, source, target, diff_pct, threshold, status, now):
    return ReconciliationResult(
        check_type="aggregate", table=table, metric=metric, source_value=source,
        target_value=target, diff_pct=diff_pct, threshold=threshold, status=status,
        environment="qa", run_timestamp=now,
    )


def test_each_history_run_links_to_a_detail_section_with_every_result(tmp_path):
    db_path = tmp_path / "results.duckdb"
    now = datetime.now(timezone.utc)
    data_load_id = write_run(
        ReconciliationRun(environment="qa", run_timestamp=now, trigger_type="data_load", results=[
            _result("vbak -> landing_vbak", "row_count", 3000.0, 3000.0, 0.0, 2.0, "pass", now),
        ]),
        db_path=db_path,
    )
    code_change_id = write_run(
        ReconciliationRun(environment="qa", run_timestamp=now, trigger_type="code_change", results=[
            _result("vbap (excl. cancelled) -> prep_sales_orders", "row_count", 7830.0, 6002.0, 23.35, 2.0, "flag", now),
            _result("vbap (excl. cancelled) -> prep_sales_orders", "sum_net_value", 50557969.69, 38621316.5, 23.61, 2.0, "flag", now),
        ]),
        db_path=db_path,
        final_classification="expected", confidence=0.93, pr_claims_no_impact=False, downgraded=False,
    )

    out = tmp_path / "index.html"
    HtmlDashboardConnector(db_path=db_path).generate_report(str(out))
    page = out.read_text(encoding="utf-8")

    for run_id in (data_load_id, code_change_id):
        assert f'href="#run-{run_id}"' in page
        section = re.search(rf'<section class="run-detail card" id="run-{run_id}">(.*?)</section>', page, re.S)
        assert section, run_id
        rows = re.findall(r"<tr class=\"(?:row-flag)?\">(.*?)</tr>", section.group(1), re.S)
        assert len(rows) == len(get_run_by_id(run_id, db_path=db_path))

    code_change_section = page.split(f'id="run-{code_change_id}"')[1]
    assert "50,557,969.69" in code_change_section and "23.61%" in code_change_section
    assert "badge-expected" in code_change_section
