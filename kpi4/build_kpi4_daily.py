#!/usr/bin/env python3
"""
KPI 4 DAILY snapshot for TWO days (today + yesterday by default).

Usage:
  python3 build_kpi4_daily.py --csv <csv> --output <html> [--combined-xlsx <xlsx>] [--date YYYY-MM-DD]

If --date is provided, that day becomes "today" and the day before becomes
"yesterday" in the report. Default: today = datetime.date.today().
"""
from __future__ import annotations

import argparse
import csv
import datetime
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from combined_file import read_all_name_tracking
except ImportError:
    read_all_name_tracking = None

DEFAULT_TEMPLATE = str(HERE.parent / "assets" / "report_template.html")

MEMBERSHIP_PLANS = {
    "Bedrock - Basic",
    "Bedrock - Plus",
    "Bedrock - Premium",
    "Multi Unit Plus Membership",
}
SALT_PLANS = {
    "Bedrock - Basic Salt Delivery Add-On",
    "Bedrock - Plus Salt Delivery Add-On",
}


def safe_date(s):
    if s is None:
        return None
    if isinstance(s, datetime.datetime):
        return s.date()
    if isinstance(s, datetime.date):
        return s
    s = str(s).strip()
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_signups(csv_rows, target, plan_set):
    out = []
    for r in csv_rows:
        if (r.get("Status") or "").strip() != "Active":
            continue
        d = safe_date(r.get("Start Date"))
        if d != target:
            continue
        plan = (r.get("Plan") or "").strip()
        if plan not in plan_set:
            continue
        out.append({
            "display_name": (r.get("Display Name") or "").strip(),
            "plan": plan,
            "start_date": d.isoformat(),
        })
    return sorted(out, key=lambda x: x["display_name"].lower())


def find_cancellations(nt_rows, target, plan_set):
    out = []
    for r in (nt_rows or []):
        ed = safe_date(r.get("end_date"))
        if ed != target:
            continue
        plan = r.get("plan") or ""
        if plan not in plan_set:
            continue
        out.append({
            "display_name": r.get("display_name") or "",
            "plan": plan,
            "start_date": r.get("start_date") or "",
            "end_date": target.isoformat(),
        })
    return sorted(out, key=lambda x: x["display_name"].lower())


def latest_signup(csv_rows, nt_rows, plan_set, on_or_before=None):
    """
    Most recent Start Date across active members in plan_set. Uses CSV (Status=Active)
    when available and falls back to Name Tracking. Returns pretty label like
    "Wed, Jul 22 2026 (Melanie Wang)" or "None on record".
    """
    best = None
    if csv_rows:
        for r in csv_rows:
            if (r.get("Status") or "").strip() != "Active":
                continue
            if (r.get("Plan") or "").strip() not in plan_set:
                continue
            d = safe_date(r.get("Start Date"))
            if not d:
                continue
            if on_or_before and d > on_or_before:
                continue
            name = (r.get("Display Name") or "").strip()
            if best is None or d > best[0]:
                best = (d, name)
    if best is None and nt_rows:
        for r in nt_rows:
            if (r.get("plan") or "") not in plan_set:
                continue
            # Skip cancelled rows
            if (r.get("end_date") or ""):
                continue
            d = safe_date(r.get("start_date"))
            if not d:
                continue
            if on_or_before and d > on_or_before:
                continue
            name = r.get("display_name") or ""
            if best is None or d > best[0]:
                best = (d, name)
    if best is None:
        return "None on record"
    d, name = best
    label = d.strftime("%a, %b %d %Y").replace(" 0", " ")
    return "%s (%s)" % (label, name) if name else label


def active_count_at(nt_rows, csv_rows, target, plan_set):
    """Total active in plan_set as of end of target. NT preferred, CSV fallback."""
    if nt_rows:
        count = 0
        for r in nt_rows:
            if (r.get("plan") or "") not in plan_set:
                continue
            sd = safe_date(r.get("start_date"))
            if not sd or sd > target:
                continue
            ed = safe_date(r.get("end_date"))
            if ed and ed <= target:
                continue
            count += 1
        return count, "Name Tracking"
    count = 0
    for r in (csv_rows or []):
        if (r.get("Status") or "").strip() != "Active":
            continue
        if (r.get("Plan") or "").strip() not in plan_set:
            continue
        d = safe_date(r.get("Start Date"))
        if not d or d > target:
            continue
        count += 1
    return count, "CSV (current-Active snapshot)"


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_signup_list(items):
    if not items:
        return '<div class="chart-note">Nobody.</div>'
    rows = ['<table class="name-list"><thead><tr><th>Name</th><th>Plan</th><th>Start</th></tr></thead><tbody>']
    for it in items:
        rows.append('<tr><td>%s</td><td>%s</td><td>%s</td></tr>' %
                    (_esc(it["display_name"]), _esc(it["plan"]), _esc(it["start_date"])))
    rows.append('</tbody></table>')
    return "\n".join(rows)


def render_cancel_list(items):
    if not items:
        return '<div class="chart-note">Nobody.</div>'
    rows = ['<table class="name-list"><thead><tr><th>Name</th><th>Plan</th><th>Start</th><th>End</th></tr></thead><tbody>']
    for it in items:
        rows.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                    (_esc(it["display_name"]), _esc(it["plan"]),
                     _esc(it["start_date"]), _esc(it["end_date"])))
    rows.append('</tbody></table>')
    return "\n".join(rows)


def number_class(n, mode):
    if n == 0:
        return "gray"
    return "orange" if mode == "cancel" else ""


def net_class_and_str(n):
    if n > 0: return "up", "+%d" % n
    if n < 0: return "down", "%d" % n
    return "flat", "0"


def pretty(d):
    return d.strftime("%A, %B %d, %Y").replace(" 0", " ")


def compute_day(prefix, target, csv_rows, nt_rows):
    """Compute all placeholders for one day. prefix = 'T' (today) or 'Y' (yesterday)."""
    m_su = find_signups(csv_rows, target, MEMBERSHIP_PLANS)
    m_cx = find_cancellations(nt_rows, target, MEMBERSHIP_PLANS) if nt_rows else []
    s_su = find_signups(csv_rows, target, SALT_PLANS)
    s_cx = find_cancellations(nt_rows, target, SALT_PLANS) if nt_rows else []
    m_net = len(m_su) - len(m_cx)
    s_net = len(s_su) - len(s_cx)
    m_cls, m_str = net_class_and_str(m_net)
    s_cls, s_str = net_class_and_str(s_net)
    return {
        "__%s_M_SU__" % prefix: str(len(m_su)),
        "__%s_M_CX__" % prefix: str(len(m_cx)),
        "__%s_S_SU__" % prefix: str(len(s_su)),
        "__%s_S_CX__" % prefix: str(len(s_cx)),
        "__%s_M_SU_CLASS__" % prefix: number_class(len(m_su), "signup"),
        "__%s_M_CX_CLASS__" % prefix: number_class(len(m_cx), "cancel"),
        "__%s_S_SU_CLASS__" % prefix: number_class(len(s_su), "signup"),
        "__%s_S_CX_CLASS__" % prefix: number_class(len(s_cx), "cancel"),
        "__%s_M_NET__" % prefix: m_str,
        "__%s_S_NET__" % prefix: s_str,
        "__%s_M_NET_CLASS__" % prefix: m_cls,
        "__%s_S_NET_CLASS__" % prefix: s_cls,
        "__%s_M_SU_LIST__" % prefix: render_signup_list(m_su),
        "__%s_S_SU_LIST__" % prefix: render_signup_list(s_su),
        "__%s_M_CX_LIST__" % prefix: render_cancel_list(m_cx),
        "__%s_S_CX_LIST__" % prefix: render_cancel_list(s_cx),
    }, {"m_su": len(m_su), "m_cx": len(m_cx), "s_su": len(s_su), "s_cx": len(s_cx)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--combined-xlsx", default=None)
    ap.add_argument("--date", default=None,
                    help="Anchor date (becomes 'today' in the report). Default: real today.")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    args = ap.parse_args()

    today = datetime.date.fromisoformat(args.date) if args.date else datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)

    csv_rows = parse_csv(args.csv)
    nt_rows = None
    cancel_note = ""
    if args.combined_xlsx:
        if read_all_name_tracking is None:
            raise SystemExit("combined_file helper missing but --combined-xlsx was set.")
        nt_rows = read_all_name_tracking(args.combined_xlsx, excluded_plans=None)
    else:
        cancel_note = ("<strong>Note:</strong> Combined File not provided, so cancellations "
                       "cannot be computed. Re-run with <code>--combined-xlsx</code> to enable them.")

    m_total, m_src = active_count_at(nt_rows, csv_rows, today, MEMBERSHIP_PLANS)
    s_total, s_src = active_count_at(nt_rows, csv_rows, today, SALT_PLANS)
    m_last = latest_signup(csv_rows, nt_rows, MEMBERSHIP_PLANS, on_or_before=today)
    s_last = latest_signup(csv_rows, nt_rows, SALT_PLANS, on_or_before=today)

    today_repl, today_metrics = compute_day("T", today, csv_rows, nt_rows)
    yest_repl, yest_metrics = compute_day("Y", yesterday, csv_rows, nt_rows)

    with open(args.template, "r", encoding="utf-8") as f:
        html = f.read()

    replacements = {
        "__TODAY_PRETTY__": pretty(today),
        "__TODAY_ISO__": today.isoformat(),
        "__YEST_PRETTY__": pretty(yesterday),
        "__YEST_ISO__": yesterday.isoformat(),
        "__CSV_NAME__": os.path.basename(args.csv),
        "__CANCEL_NOTE__": cancel_note,
        "__REPORT_GENERATED__": datetime.date.today().isoformat(),
        "__M_TOTAL__": str(m_total),
        "__S_TOTAL__": str(s_total),
        "__M_TOTAL_SOURCE__": _esc(m_src),
        "__S_TOTAL_SOURCE__": _esc(s_src),
        "__M_LAST_SIGNUP__": _esc(m_last),
        "__S_LAST_SIGNUP__": _esc(s_last),
    }
    replacements.update(today_repl)
    replacements.update(yest_repl)
    for k, v in replacements.items():
        html = html.replace(k, v)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    print("Wrote %s (%d bytes)" % (args.output, len(html)))
    print("Anchor today=%s, yesterday=%s" % (today, yesterday))
    print("Totals (source=%s): memberships=%d, salt=%d" % (m_src, m_total, s_total))
    print("Last signup: memberships=%s | salt=%s" % (m_last, s_last))
    print("TODAY:     M signups=%d cancels=%d | S signups=%d cancels=%d" %
          (today_metrics["m_su"], today_metrics["m_cx"], today_metrics["s_su"], today_metrics["s_cx"]))
    print("YESTERDAY: M signups=%d cancels=%d | S signups=%d cancels=%d" %
          (yest_metrics["m_su"], yest_metrics["m_cx"], yest_metrics["s_su"], yest_metrics["s_cx"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
