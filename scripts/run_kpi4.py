#!/usr/bin/env python3
"""
Render Cron Job entrypoint.

Runs weekly. Pulls the newest CSV from the Drive folder, downloads the current
Combined File, runs the KPI 4 skill to produce (a) an updated Combined File
and (b) an HTML report. Replaces the Combined File in Drive (preserving its ID
and sharing URL) and uploads the report next to it.

Env vars required:
  GOOGLE_SERVICE_ACCOUNT   JSON service-account key
  DRIVE_CSV_FOLDER_ID      Folder where fetch_hcp_csv.py drops CSVs
  COMBINED_FILE_ID         Google Sheet ID of the Combined File
  EXCLUDE_PLANS            optional CSV of plan names to drop (default baked-in)
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "kpi4"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from drive_client import (  # noqa: E402
    get_service,
    latest_csv_in_folder,
    download_file,
    export_google_file,
    replace_google_sheet_from_xlsx,
    upload_file,
)

# The skill's own module (copied under kpi4/) does the heavy lifting.
import build_kpi4_report as bkr  # noqa: E402


SHEET_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit("Missing required env var: %s" % name)
    return v


def main() -> int:
    csv_folder_id = require_env("DRIVE_CSV_FOLDER_ID")
    combined_id = require_env("COMBINED_FILE_ID")
    exclude_plans = os.environ.get(
        "EXCLUDE_PLANS",
        ",".join(sorted(bkr.DEFAULT_EXCLUDED_PLANS)),
    )

    service = get_service()

    # 1. Pull the newest CSV from Drive
    latest = latest_csv_in_folder(service, csv_folder_id)
    if not latest:
        raise SystemExit("No CSV found in DRIVE_CSV_FOLDER_ID=%s" % csv_folder_id)
    print("Latest CSV: %s (modified %s)" % (latest["name"], latest["modifiedTime"]))
    csv_local = "/tmp/latest.csv"
    download_file(service, latest["id"], csv_local)

    # 2. Export current Combined File as .xlsx so we can read + modify it
    combined_local = "/tmp/combined_current.xlsx"
    export_google_file(service, combined_id, SHEET_MIME, combined_local)

    # 3. Run the skill
    today = datetime.date.today()
    pull_date = bkr.extract_pull_date(latest["name"])
    updated_combined = "/tmp/combined_updated.xlsx"
    report_local = "/tmp/kpi4_report_%s.html" % pull_date
    template = str(REPO_ROOT / "kpi4" / "assets" / "report_template.html")

    excluded_set = {p.strip() for p in exclude_plans.split(",") if p.strip()}
    all_rows = bkr.parse_csv(csv_local)
    kept = bkr.filter_rows(all_rows, {"Active"}, None, excluded_plans=excluded_set)

    print("Running Combined File update...")
    from combined_file import update_combined_file, read_cancellations, read_all_name_tracking
    diff = update_combined_file(
        combined_local, all_rows, pull_date, updated_combined,
        excluded_plans=excluded_set,
    )
    print("  New snapshot tab: %s" % diff["new_tab_name"])
    print("  Cancelled this run: %d" % len(diff["cancelled_this_run"]))
    print("  New this run: %d" % len(diff["new_this_run"]))

    print("Building KPI 4 report...")
    agg = bkr.aggregate(kept, today, 13)
    cancellations = read_cancellations(updated_combined, excluded_plans=excluded_set)
    weekly_cx = bkr.aggregate_weekly_cancellations(cancellations, today, 13, agg["plans"])
    nt_rows = read_all_name_tracking(updated_combined, excluded_plans=excluded_set)
    nt_cum = bkr.aggregate_nt_cumulative(nt_rows, today, 13, agg["plans"])
    html = bkr.render_html(
        agg, os.path.basename(csv_local), template,
        diff=diff, excluded_plans=excluded_set,
        weekly_cancellations=weekly_cx, nt_cumulative=nt_cum,
    )
    with open(report_local, "w", encoding="utf-8") as f:
        f.write(html)
    print("Report written to %s (%d bytes)" % (report_local, len(html)))

    # 4. Push results back to Drive
    print("Replacing Combined File in Drive (preserves ID / URL)...")
    replace_google_sheet_from_xlsx(service, combined_id, updated_combined)

    # Uploading the HTML report as a NEW Drive file requires storage quota, which
    # service accounts don't have unless the target folder lives on a Shared Drive.
    # Try it, but don't fail the whole run if it errors — the sheet update above is
    # the important side effect.
    try:
        print("Uploading HTML report to Drive folder...")
        upload_file(service, report_local, csv_folder_id, mime_type="text/html")
    except Exception as e:
        print("Report upload skipped (service account lacks storage quota): %s" % e)
        print("The Combined File in Drive is still updated. Generate the HTML report")
        print("on demand via the Cowork skill, or move the folder to a Shared Drive.")

    print("Done. Total active: %d" % agg["summary"]["total_active"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
