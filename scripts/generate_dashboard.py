"""CI entry point for the dashboard-publishing workflow
(.github/workflows/publish_dashboard.yml). Wraps
connectors/reporting/html_dashboard.py -- no reconciliation/agent logic
lives here, matching CLAUDE.md's dashboard boundary rule (reads only from
results_store, never live reconciliation/agent output).

Usage: python scripts/generate_dashboard.py [output_path]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.reporting.html_dashboard import HtmlDashboardConnector

DEFAULT_OUTPUT = "public/index.html"


def main() -> None:
    output_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    HtmlDashboardConnector().generate_report(output_path)
    print(f"Generated dashboard at {output_path}")


if __name__ == "__main__":
    main()
