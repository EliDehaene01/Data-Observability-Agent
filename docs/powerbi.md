# Power BI companion (local)

A local Power BI Project (PBIP / TMDL, all text files) that reads the same
`results_store/` data as the HTML dashboard, for people who'd rather slice
it in Power BI. It lives in [`powerbi/`](../powerbi/):

```
powerbi/
  DataObservabilityAgent.pbip                     ← open this in Power BI Desktop
  DataObservabilityAgent.SemanticModel/           ← model as TMDL (tables, measures, M queries)
  DataObservabilityAgent.Report/                  ← report shell, one empty "Overview" page
  data/                                           ← exported CSVs (gitignored, regenerated)
```

## Why it's a manual/local artifact, not CI

Power BI Desktop is Windows-GUI-only — it can't run headless, so there is no
way for a GitHub Actions job to open the `.pbip`, refresh it, and publish a
`.pbix`. Everything below is a manual step you run on a machine with Power BI
Desktop installed.

This is the **same class of deliberate MVP limitation** as
`post_slack_notification` being a one-way notification rather than a
human-in-the-loop approval step (see
[`architecture.md`](architecture.md) §6): the capability that would make it
automated (a hosted callback there; a headless Power BI runtime here) doesn't
exist in this project, so the honest move is to ship the useful local version
and document the ceiling rather than pretend it's wired into CI.

The **automated** reporting artifact is the HTML dashboard
(`connectors/reporting/html_dashboard.py`, published to GitHub Pages by
`publish_dashboard.yml`). The Power BI companion complements it for ad-hoc
exploration; it does not replace it.

## 1. Regenerate the CSVs

```bash
uv run python scripts/export_results_for_powerbi.py
```

Fetches the current `results_store/results.duckdb` from the `data-results`
branch (`main` no longer tracks it — see
[`architecture.md`](architecture.md#8-results-store-and-dashboard)), reads it
via `results_store.reader` (the same read-only path the dashboard uses — no
reconciliation or agent code), and writes:

| file | grain | columns |
|---|---|---|
| `powerbi/data/reconciliation_results.csv` | one row per `ReconciliationResult` (pass + flag, both triggers) | the full `results` table |
| `powerbi/data/classification_history.csv` | one row per `trigger_type="code_change"` run | run metadata + `final_classification`, `confidence`, `pr_claims_no_impact`, `downgraded`, `total_checks`, `flagged_checks` |

Both are gitignored — they're regenerated data, not source. Timestamps are
written as `YYYY-MM-DD HH:MM:SS`, booleans as lowercase `true`/`false`.

Pass a directory argument to write them somewhere else:
`python scripts/export_results_for_powerbi.py /some/dir`.

## 2. Open the project

Open `powerbi/DataObservabilityAgent.pbip` in **Power BI Desktop**
(File → Open, or double-click). Power BI reads the TMDL model and the report
shell directly from the text files.

> **First open only:** set the `CsvFolder` parameter. Transform data → Manage
> parameters → `CsvFolder` → set it to the absolute path of your checkout's
> `powerbi/data/` folder, **with a trailing backslash**, e.g.
> `C:\src\Data-Observability-Agent\powerbi\data\`. The default in
> `DataObservabilityAgent.SemanticModel/definition/expressions.tmdl` points at
> one specific machine; yours will differ.

## 3. Refresh

Home → **Refresh**. This re-reads the two CSVs. Re-run step 1 whenever the
results store changes and hit Refresh again — there is no automatic refresh.

## Model contents

Two tables, matching the two CSVs 1:1. No relationship between them (they're
at different grains; `classification_history` is a pre-aggregated view of the
`code_change` slice of `reconciliation_results`).

**Measures on `reconciliation_results`:**

| measure | definition |
|---|---|
| `Flag Rate %` | share of result rows with `status = "flag"` |
| `Runs by Environment` | `DISTINCTCOUNT(run_id)` — put `environment` on an axis |

**Measures on `classification_history`:**

| measure | definition |
|---|---|
| `Expected count` / `Needs Review count` / `Anomaly count` | runs at each `final_classification` |
| `Average Confidence` | `AVERAGE(confidence)` |
| `PR-Honesty Override Fire Count` | runs where `pr_claims_no_impact = TRUE` **and** `downgraded = TRUE` — i.e. the deterministic PR-honesty override (Rule 2, [`architecture.md`](architecture.md) §5) actually fired |

The report has a single blank `Overview` page — build whatever visuals you
want on top of the model.

## 4. Save As `.pbix` (only if you need a compiled binary)

The PBIP text files are the source of truth and are what's committed. You only
need a `.pbix` if something requires the single-file binary — most commonly
**publishing to the Power BI Service** (app.powerbi.com), which doesn't accept
PBIP folders.

In Power BI Desktop: **File → Save As →** choose *Power BI files (\*.pbix)* →
save it **outside** `powerbi/` (or just let it be — `*.pbix` isn't gitignored,
but don't commit it; the `.pbip` is the artifact). Then
**Home → Publish** to push it to a workspace.

Regenerating from source: re-open the `.pbip`, Refresh, Save As `.pbix` again.
