"""Static HTML implementation of ReportingConnector. Reads only from
results_store (see connectors/reporting/base.py); never touches live
reconciliation output or agent state. No build step, no JS framework --
one self-contained HTML file with embedded CSS, bars drawn with plain
flexbox divs.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from connectors.reporting.base import ReportingConnector
from results_store.reader import get_all_results, get_recent_runs

_STATUS_LABEL = {"pass": "PASS", "flag": "FLAG"}
_CLASSIFICATION_CLASS = {
    "expected": "badge-expected",
    "needs_review": "badge-needs-review",
    "anomaly": "badge-anomaly",
}


def _summarize_runs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group flat result rows (newest-first, as returned by get_all_results)
    into one summary dict per run_id, preserving that same newest-first
    order."""
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        run_id = row["run_id"]
        if run_id not in grouped:
            grouped[run_id] = []
            order.append(run_id)
        grouped[run_id].append(row)

    summaries = []
    for run_id in order:
        run_rows = grouped[run_id]
        first = run_rows[0]
        flagged = sum(1 for r in run_rows if r["status"] == "flag")
        summaries.append(
            {
                "run_id": run_id,
                "environment": first["environment"],
                "trigger_type": first["trigger_type"],
                "run_timestamp": first["run_timestamp"],
                "total_checks": len(run_rows),
                "flagged_checks": flagged,
                "overall_status": "flag" if flagged else "pass",
                "final_classification": first.get("final_classification"),
                "confidence": first.get("confidence"),
                "pr_claims_no_impact": first.get("pr_claims_no_impact"),
                "downgraded": first.get("downgraded"),
            }
        )
    return summaries


def _counts_by_environment(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        env = row["environment"]
        counts.setdefault(env, {"pass": 0, "flag": 0})
        counts[env][row["status"]] += 1
    return counts


def _most_recent_per_trigger(summaries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    most_recent: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        most_recent.setdefault(summary["trigger_type"], summary)
    return most_recent


def _format_timestamp(value: Any) -> str:
    try:
        return value.strftime("%Y-%m-%d %H:%M UTC")
    except AttributeError:
        return html.escape(str(value))


def _render_bar(pass_count: int, flag_count: int) -> str:
    total = pass_count + flag_count
    if total == 0:
        return '<div class="bar bar-empty"></div>'
    pass_pct = pass_count / total * 100
    flag_pct = flag_count / total * 100
    return (
        '<div class="bar">'
        f'<div class="bar-pass" style="width:{pass_pct:.2f}%"></div>'
        f'<div class="bar-flag" style="width:{flag_pct:.2f}%"></div>'
        "</div>"
    )


def _render_status_badge(status: str) -> str:
    css_class = "badge-pass" if status == "pass" else "badge-flag"
    return f'<span class="badge {css_class}">{_STATUS_LABEL.get(status, status.upper())}</span>'


def _render_classification_badge(classification: str | None) -> str:
    if not classification:
        return '<span class="muted">&mdash;</span>'
    css_class = _CLASSIFICATION_CLASS.get(classification, "badge-pass")
    return f'<span class="badge {css_class}">{html.escape(classification)}</span>'


def _render_summary_section(
    counts_by_env: dict[str, dict[str, int]],
    most_recent_by_trigger: dict[str, dict[str, Any]],
) -> str:
    env_rows = []
    for env in sorted(counts_by_env):
        counts = counts_by_env[env]
        env_rows.append(
            f"""
            <div class="summary-row">
                <div class="summary-env">{html.escape(env)}</div>
                {_render_bar(counts["pass"], counts["flag"])}
                <div class="summary-counts">
                    <span class="badge-pass-text">{counts["pass"]} pass</span>
                    <span class="badge-flag-text">{counts["flag"]} flag</span>
                </div>
            </div>
            """
        )

    trigger_cards = []
    for trigger_type, label in (("data_load", "Data-load check"), ("code_change", "Code-change check")):
        run = most_recent_by_trigger.get(trigger_type)
        if run is None:
            trigger_cards.append(
                f'<div class="card trigger-card"><h3>{label}</h3><p class="muted">No runs yet.</p></div>'
            )
            continue
        trigger_cards.append(
            f"""
            <div class="card trigger-card">
                <h3>{label}</h3>
                <p>{_render_status_badge(run["overall_status"])} in <strong>{html.escape(run["environment"])}</strong></p>
                <p class="muted">{_format_timestamp(run["run_timestamp"])}</p>
                <p class="muted">{run["flagged_checks"]}/{run["total_checks"]} checks flagged</p>
            </div>
            """
        )

    return f"""
    <section>
        <h2>Summary</h2>
        <div class="card-row">
            {"".join(trigger_cards)}
        </div>
        <div class="card">
            <h3>Pass / flag counts by environment</h3>
            {"".join(env_rows) if env_rows else '<p class="muted">No results yet.</p>'}
        </div>
    </section>
    """


def _render_data_load_history(summaries: list[dict[str, Any]]) -> str:
    rows = [s for s in summaries if s["trigger_type"] == "data_load"][:20]
    if not rows:
        return '<section><h2>Data-load check history</h2><p class="muted">No data-load runs yet.</p></section>'

    body_rows = "".join(
        f"""
        <tr>
            <td>{_format_timestamp(r["run_timestamp"])}</td>
            <td>{html.escape(r["environment"])}</td>
            <td>{_render_status_badge(r["overall_status"])}</td>
            <td>{r["flagged_checks"]}/{r["total_checks"]}</td>
        </tr>
        """
        for r in rows
    )
    return f"""
    <section>
        <h2>Data-load check history</h2>
        <table>
            <thead>
                <tr><th>Run</th><th>Environment</th><th>Status</th><th>Flagged</th></tr>
            </thead>
            <tbody>{body_rows}</tbody>
        </table>
    </section>
    """


def _render_code_change_history(summaries: list[dict[str, Any]]) -> str:
    rows = [s for s in summaries if s["trigger_type"] == "code_change"][:20]
    if not rows:
        return '<section><h2>Code-change check history</h2><p class="muted">No code-change runs yet.</p></section>'

    body_rows = []
    for r in rows:
        confidence = r["confidence"]
        confidence_text = f"{confidence:.2f}" if confidence is not None else "&mdash;"
        downgraded_text = "yes" if r["downgraded"] else "no"
        no_impact_text = "yes" if r["pr_claims_no_impact"] else "no"
        body_rows.append(
            f"""
            <tr>
                <td>{_format_timestamp(r["run_timestamp"])}</td>
                <td>{html.escape(r["environment"])}</td>
                <td>{_render_classification_badge(r["final_classification"])}</td>
                <td>{confidence_text}</td>
                <td>{no_impact_text}</td>
                <td>{downgraded_text}</td>
                <td>{r["flagged_checks"]}/{r["total_checks"]}</td>
            </tr>
            """
        )
    return f"""
    <section>
        <h2>Code-change check history</h2>
        <table>
            <thead>
                <tr>
                    <th>Run</th><th>Environment</th><th>Classification</th>
                    <th>Confidence</th><th>PR claims no impact</th><th>Downgraded</th><th>Flagged</th>
                </tr>
            </thead>
            <tbody>{"".join(body_rows)}</tbody>
        </table>
    </section>
    """


_CSS = """
:root {
    color-scheme: light;
    --bg: #f7f7f8;
    --card-bg: #ffffff;
    --border: #e2e2e6;
    --text: #1a1a1a;
    --muted: #6b7280;
    --pass: #16a34a;
    --flag: #dc2626;
    --needs-review: #d97706;
    --accent: #2563eb;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    padding: 2rem 1.5rem 4rem;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
}
main { max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.75rem; margin-bottom: 0.25rem; }
h2 { font-size: 1.25rem; margin: 2rem 0 0.75rem; }
h3 { font-size: 1rem; margin: 0 0 0.5rem; }
.subtitle { color: var(--muted); margin-top: 0; }
.card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1rem 1.25rem;
    margin-bottom: 1rem;
}
.card-row { display: flex; gap: 1rem; flex-wrap: wrap; }
.trigger-card { flex: 1 1 240px; }
table { width: 100%; border-collapse: collapse; background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
th { color: var(--muted); font-weight: 600; background: var(--bg); }
tr:last-child td { border-bottom: none; }
.muted { color: var(--muted); }
.badge {
    display: inline-block;
    padding: 0.15rem 0.55rem;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    color: #fff;
}
.badge-pass { background: var(--pass); }
.badge-flag { background: var(--flag); }
.badge-expected { background: var(--pass); }
.badge-needs-review { background: var(--needs-review); }
.badge-anomaly { background: var(--flag); }
.badge-pass-text { color: var(--pass); font-weight: 600; font-size: 0.85rem; }
.badge-flag-text { color: var(--flag); font-weight: 600; font-size: 0.85rem; margin-left: 0.75rem; }
.summary-row { display: flex; align-items: center; gap: 1rem; padding: 0.4rem 0; }
.summary-env { width: 90px; font-weight: 600; }
.bar { flex: 1; display: flex; height: 10px; border-radius: 6px; overflow: hidden; background: var(--border); }
.bar-empty { background: var(--border); }
.bar-pass { background: var(--pass); }
.bar-flag { background: var(--flag); }
.summary-counts { width: 140px; text-align: right; white-space: nowrap; }
footer { color: var(--muted); font-size: 0.8rem; margin-top: 2rem; }
"""


class HtmlDashboardConnector(ReportingConnector):
    def generate_report(self, output_path: str) -> None:
        all_rows = get_all_results()
        summaries = _summarize_runs(all_rows)
        counts_by_env = _counts_by_environment(all_rows)
        most_recent_by_trigger = _most_recent_per_trigger(summaries)
        # get_recent_runs isn't strictly needed once we have all_rows, but
        # exercising it here keeps this connector honest that both read
        # functions actually work against the real store.
        get_recent_runs(limit=1)

        body = (
            _render_summary_section(counts_by_env, most_recent_by_trigger)
            + _render_data_load_history(summaries)
            + _render_code_change_history(summaries)
        )

        html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data Observability Agent — Dashboard</title>
<style>{_CSS}</style>
</head>
<body>
<main>
    <h1>Data Observability Agent</h1>
    <p class="subtitle">Reconciliation history, read from results_store.</p>
    {body}
    <footer>Generated by connectors/reporting/html_dashboard.py from results_store/results.duckdb.</footer>
</main>
</body>
</html>
"""
        Path(output_path).write_text(html_doc, encoding="utf-8")
