#!/usr/bin/env python3
"""
Build the KPI 4 Membership dashboard HTML from a service-agreements CSV export.

Usage:
  python3 build_kpi4_report.py --csv <csv> --output <html>

Optional flags:
  --weeks N                Number of trailing weeks (default: 13)
  --include-status A,B     Statuses for the signup chart (default: Active).
                           The cumulative growth chart always uses Active only.
  --plans "A,B"            Restrict charts to these plan names.
  --today YYYY-MM-DD       Override "today" (for testing).
  --template PATH          Override the HTML template (default: ../assets/report_template.html)
"""
import argparse
import csv
import datetime
import os
import sys
from collections import Counter

try:
    from combined_file import extract_pull_date, update_combined_file, read_cancellations, read_all_name_tracking
except ImportError:
    # Allow running with the module path prepended manually
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from combined_file import extract_pull_date, update_combined_file, read_cancellations, read_all_name_tracking

# Editorial fallback palette (used only if a plan isn't explicitly mapped below).
PALETTE = ["#1B1B18", "#C44819", "#7A7466", "#4A4844",
           "#B85C2E", "#9A9282", "#2C2C28", "#D4D0C4"]

# Explicit plan -> color mapping (business colors).
PLAN_COLORS = {
    "Bedrock - Basic": "#2563EB",           # blue
    "Bedrock - Plus": "#EAB308",            # yellow
    "Bedrock - Premium": "#7C3AED",         # purple
    "Multi Unit Plus Membership": "#DC2626",# red
}


def plan_color(plan, i=0):
    return PLAN_COLORS.get(plan, PALETTE[i % len(PALETTE)])

# Plans that are always excluded from KPI 4 (charts, plan mix, active count, tracking diff).
# Salt Delivery add-ons are billing add-ons, not stand-alone memberships.
# Free Plus and Non-Renewing plans are complimentary/promo and don't count toward recurring membership.
DEFAULT_EXCLUDED_PLANS = {
    "Bedrock - Plus Salt Delivery Add-On",
    "Bedrock - Basic Salt Delivery Add-On",
    "Bedrock Basic (1 year Non-Renewing)",
    "Free Plus Membership",
}

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.normpath(os.path.join(HERE, "..", "assets", "report_template.html"))


def week_start(d):
    # Friday-anchored weeks: each bucket runs Fri -> following Thu.
    # weekday(): Mon=0, Tue=1, ..., Fri=4, Sat=5, Sun=6
    return d - datetime.timedelta(days=(d.weekday() - 4) % 7)


def parse_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def filter_rows(rows, allowed_statuses, allowed_plans, excluded_plans=None):
    excluded_plans = excluded_plans or set()
    out = []
    for r in rows:
        if (r.get("Status") or "").strip() not in allowed_statuses:
            continue
        plan = (r.get("Plan") or "").strip()
        if plan in excluded_plans:
            continue
        if allowed_plans is not None and plan not in allowed_plans:
            continue
        out.append(r)
    return out


def safe_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def aggregate(rows, today, weeks):
    active = [r for r in rows if (r.get("Status") or "").strip() == "Active"]
    plan_set = sorted({(r.get("Plan") or "").strip() for r in active if (r.get("Plan") or "").strip()})

    this_week_monday = week_start(today)
    week_mondays = [this_week_monday - datetime.timedelta(days=(weeks - 1 - i) * 7) for i in range(weeks)]

    weekly = {wm: {p: 0 for p in plan_set} for wm in week_mondays}
    for r in rows:
        d = safe_date(r.get("Start Date"))
        if not d:
            continue
        wm = week_start(d)
        if wm not in weekly:
            continue
        plan = (r.get("Plan") or "").strip()
        if plan in plan_set:
            weekly[wm][plan] += 1

    weekly_serialized = [{
        "label": wm.strftime("%b %d"),
        "counts": weekly[wm],
        "total": sum(weekly[wm].values()),
    } for wm in week_mondays]

    # Cumulative growth chart now spans the SAME last-N-weeks window as the signup chart,
    # not full year-to-date. Cumulative counts are still computed from the full history.
    ytd_serialized = []
    for wm in week_mondays:
        we = wm + datetime.timedelta(days=6)
        cum = {p: 0 for p in plan_set}
        for r in active:
            d = safe_date(r.get("Start Date"))
            if not d or d > we:
                continue
            ed = safe_date(r.get("End Date"))
            if ed and ed <= we:
                continue
            plan = (r.get("Plan") or "").strip()
            if plan in cum:
                cum[plan] += 1
        ytd_serialized.append({
            "label": wm.strftime("%b %d"),
            "cum_counts": cum,
            "cum_total": sum(cum.values()),
        })

    plan_counts = Counter((r.get("Plan") or "").strip() for r in active)
    plan_counts = {p: plan_counts.get(p, 0) for p in plan_set}

    all_dates = [d for d in (safe_date(r.get("Start Date")) for r in active) if d]
    summary = {
        "total_active": len(active),
        "total_records": len(rows),
        "plan_breakdown": plan_counts,
        "report_generated": today.isoformat(),
        "earliest_signup": min(all_dates).isoformat() if all_dates else "",
    }

    return {"summary": summary, "plans": plan_set, "weekly": weekly_serialized, "ytd_weekly_cum": ytd_serialized}


def render_net_svg(weekly, weekly_cx):
    """
    Per-week NET (signups - cancellations). Positive bars go up (ink), negative go down (orange).
    """
    W, H = 880, 300
    ml, mr, mt, mb = 55, 20, 20, 60
    cw, ch = W - ml - mr, H - mt - mb
    n = len(weekly)
    cx_map = {w["label"]: w["total"] for w in (weekly_cx or [])}
    nets = [w["total"] - cx_map.get(w["label"], 0) for w in weekly]
    max_up = max([v for v in nets if v > 0] + [1])
    max_dn = max([-v for v in nets if v < 0] + [1])

    def nice_max(v):
        if v <= 5: return 5
        if v <= 10: return 10
        if v <= 15: return 15
        if v <= 20: return 20
        return ((v // 5) + 1) * 5

    y_up = nice_max(max_up)
    y_dn = nice_max(max_dn)
    total_range = y_up + y_dn
    up_h = ch * y_up / total_range
    dn_h = ch * y_dn / total_range
    zero_y = mt + up_h
    bar_w = cw / n * 0.7
    gap = cw / n - bar_w

    out = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit;">' % (W, H)]
    # Grid + labels
    for i in range(4):
        yv = y_up * (i + 1) / 4
        y = zero_y - up_h * (i + 1) / 4
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#E8E3D8" stroke-width="1"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="#7A7466">+%d</text>' % (ml - 8, y + 4, int(round(yv))))
    for i in range(4):
        yv = y_dn * (i + 1) / 4
        y = zero_y + dn_h * (i + 1) / 4
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#E8E3D8" stroke-width="1"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="#7A7466">-%d</text>' % (ml - 8, y + 4, int(round(yv))))
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#1B1B18" stroke-width="1.5"/>' % (ml, zero_y, W - mr, zero_y))
    out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" font-weight="600" fill="#1B1B18">0</text>' % (ml - 8, zero_y + 4))

    for i, wk in enumerate(weekly):
        x = ml + i * (cw / n) + gap / 2
        v = nets[i]
        if v >= 0:
            h = up_h * v / y_up
            y = zero_y - h
            fill = "#1B1B18"
        else:
            h = dn_h * (-v) / y_dn
            y = zero_y
            fill = "#C44819"
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"><title>%s: net %+d</title></rect>' %
                   (x, y, bar_w, h, fill, wk["label"], v))
        label_y = (y - 5) if v >= 0 else (y + h + 13)
        label_color = "#1B1B18" if v >= 0 else "#C44819"
        sign = "+" if v > 0 else ("-" if v < 0 else "")
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="600" fill="%s">%s%d</text>' %
                   (x + bar_w / 2, label_y, label_color, sign, abs(v)))
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" fill="#7A7466" transform="rotate(-35 %.1f %.1f)">%s</text>' %
                   (x + bar_w / 2, H - mb + 18, x + bar_w / 2, H - mb + 18, wk["label"]))
    out.append('</svg>')
    return "\n".join(out)


def aggregate_nt_cumulative(nt_rows, today, weeks, plans):
    """
    For each of the last N Fri-Thu weeks, count Name Tracking members active at the week's end:
      start_date <= week_end AND (end_date empty OR end_date > week_end)
    Empty end_date is treated as "still active" (they haven't left as of today), so they
    count for every week up to and including the current one, even if the week's end is
    slightly in the future.
    """
    this_week_monday = week_start(today)
    week_mondays = [this_week_monday - datetime.timedelta(days=(weeks - 1 - i) * 7) for i in range(weeks)]
    out = []
    for wm in week_mondays:
        we = wm + datetime.timedelta(days=6)
        cum = {p: 0 for p in plans}
        other = 0
        for r in nt_rows:
            sd = safe_date(r.get("start_date"))
            if not sd or sd > we:
                continue
            ed_raw = (r.get("end_date") or "").strip()
            if ed_raw:
                ed = safe_date(ed_raw)
                if ed and ed <= we:
                    continue
            plan = r.get("plan") or ""
            if plan in cum:
                cum[plan] += 1
            else:
                other += 1
        if other > 0:
            cum.setdefault("Other", 0)
            cum["Other"] += other
        out.append({
            "label": wm.strftime("%b %d"),
            "cum_counts": cum,
            "cum_total": sum(cum.values()),
        })
    return out


def render_nt_cumulative_section(nt_cum, plans, has_data):
    """Section F — cumulative actives sourced from Name Tracking."""
    if not has_data:
        return ('<div class="section-header"><div class="section-badge">F</div>'
                '<div class="section-title">Cumulative actives (Name Tracking)</div>'
                '<div class="section-rule"></div></div>'
                '<div class="chart-block"><div class="chart-note">'
                'Combined File not provided, so this chart cannot render. '
                'Re-run with <code>--combined-xlsx</code> to enable it.</div></div>')
    svg = render_ytd_svg(nt_cum, plans)
    legend = render_legend(plans)
    total = nt_cum[-1]["cum_total"] if nt_cum else 0
    first = nt_cum[0]["cum_total"] if nt_cum else 0
    return (
        '<div class="section-header"><div class="section-badge">F</div>'
        '<div class="section-title">Cumulative actives (Name Tracking)</div>'
        '<div class="section-rule"></div></div>'
        '<div class="chart-block">'
        '<div class="chart-title">Historical actives &middot; last %d weeks</div>'
        '<div class="chart-note">Count sourced from the Combined File\'s Name Tracking tab. A member counts as active in a given week if their Start Date is on or before the week\'s end and their End Date is empty or later than the week\'s end (empty End Date treated as today). This includes members who have since cancelled but were active in that week. Endpoint: %d &rarr; %d.</div>'
        '%s %s'
        '</div>'
    ) % (len(nt_cum), first, total, svg, legend)


def render_weekly_diverging_svg(weekly, weekly_cx, plans):
    """
    Diverging bar chart: signups stack upward (ink palette), cancellations stack
    downward (orange palette). Both use the same 13-week Fri-Thu window.
    """
    W, H = 880, 420
    ml, mr, mt, mb = 55, 20, 20, 60
    cw, ch = W - ml - mr, H - mt - mb
    n = len(weekly)
    max_up = max((w["total"] for w in weekly), default=1)
    max_dn = max((w["total"] for w in (weekly_cx or [])), default=0)
    if max_dn == 0:
        max_dn = 1  # keep axis symmetric-ish

    def nice_max(v):
        if v <= 5: return 5
        if v <= 10: return 10
        if v <= 15: return 15
        if v <= 20: return 20
        return ((v // 5) + 1) * 5

    y_up = nice_max(max_up)
    y_dn = nice_max(max_dn)
    # scale factor: total axis height = ch; split proportional to up vs dn
    total_range = y_up + y_dn
    up_h = ch * y_up / total_range
    dn_h = ch * y_dn / total_range
    zero_y = mt + up_h
    bar_w = cw / n * 0.7
    gap = cw / n - bar_w

    out = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit;">' % (W, H)]

    # Diagonal-stripe patterns, one per plan color, for the (negative) cancellation bars.
    out.append('<defs>')
    seen_pids = set()
    for pi, p in enumerate(plans):
        col = plan_color(p, pi)
        pid = "stripe_%s" % col.lstrip("#")
        if pid in seen_pids: continue
        seen_pids.add(pid)
        out.append(
            '<pattern id="%s" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
            '<rect width="6" height="6" fill="%s"/>'
            '<line x1="0" y1="0" x2="0" y2="6" stroke="#F5F1E8" stroke-width="2"/>'
            '</pattern>' % (pid, col)
        )
    out.append('</defs>')

    # Grid lines & labels (positive side)
    for i in range(4):
        yv = y_up * (i + 1) / 4
        y = zero_y - up_h * (i + 1) / 4
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#E8E3D8" stroke-width="1"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="#7A7466">%d</text>' % (ml - 8, y + 4, int(round(yv))))
    # Grid lines & labels (negative side)
    for i in range(4):
        yv = y_dn * (i + 1) / 4
        y = zero_y + dn_h * (i + 1) / 4
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#E8E3D8" stroke-width="1"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="#7A7466">-%d</text>' % (ml - 8, y + 4, int(round(yv))))
    # Zero line (bold)
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#1B1B18" stroke-width="1.5"/>' % (ml, zero_y, W - mr, zero_y))
    out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" font-weight="600" fill="#1B1B18">0</text>' % (ml - 8, zero_y + 4))

    # Signup bars (up) — solid plan colors, with per-segment count when segment is tall enough.
    for i, wk in enumerate(weekly):
        x = ml + i * (cw / n) + gap / 2
        y_cursor = zero_y
        for pi, p in enumerate(plans):
            v = wk["counts"].get(p, 0)
            if v <= 0: continue
            h = up_h * v / y_up
            y_cursor -= h
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"><title>%s - %s: +%d</title></rect>' %
                       (x, y_cursor, bar_w, h, plan_color(p, pi), wk["label"], p, v))
            if h >= 14:
                out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10" font-weight="600" fill="#F5F1E8" stroke="#1B1B18" stroke-width="0.4" paint-order="stroke">%d</text>' %
                           (x + bar_w / 2, y_cursor + h / 2 + 3, v))
        if wk["total"] > 0:
            ty = zero_y - up_h * wk["total"] / y_up
            out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="600" fill="#1B1B18">%d</text>' %
                       (x + bar_w / 2, ty - 5, wk["total"]))

    # Cancellation bars (down)
    weekly_cx = weekly_cx or []
    cx_map = {w["label"]: w for w in weekly_cx}
    for i, wk in enumerate(weekly):
        x = ml + i * (cw / n) + gap / 2
        cx = cx_map.get(wk["label"], {"counts": {}, "total": 0})
        y_cursor = zero_y
        for pi, p in enumerate(plans):
            v = cx["counts"].get(p, 0)
            if v <= 0: continue
            h = dn_h * v / y_dn
            col = plan_color(p, pi)
            pid = "stripe_%s" % col.lstrip("#")
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="url(#%s)" stroke="%s" stroke-width="0.6"><title>%s - %s: -%d</title></rect>' %
                       (x, y_cursor, bar_w, h, pid, col, wk["label"], p, v))
            if h >= 14:
                out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10" font-weight="600" fill="#F5F1E8" stroke="#1B1B18" stroke-width="0.4" paint-order="stroke">%d</text>' %
                           (x + bar_w / 2, y_cursor + h / 2 + 3, v))
            y_cursor += h
        if cx["total"] > 0:
            ty = zero_y + dn_h * cx["total"] / y_dn
            out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="600" fill="#C44819">-%d</text>' %
                       (x + bar_w / 2, ty + 13, cx["total"]))
        # x-axis label at bottom
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" fill="#7A7466" transform="rotate(-35 %.1f %.1f)">%s</text>' %
                   (x + bar_w / 2, H - mb + 18, x + bar_w / 2, H - mb + 18, wk["label"]))

    out.append('</svg>')

    # Legend: solid = signups, striped = cancellations. One row per plan.
    legend = ['<div class="legend" style="margin-top:14px;">']
    for i, p in enumerate(plans):
        col = plan_color(p, i)
        # solid swatch + striped swatch side-by-side
        stripe_bg = (
            "linear-gradient(45deg, %s 25%%, #F5F1E8 25%%, #F5F1E8 50%%, "
            "%s 50%%, %s 75%%, #F5F1E8 75%%, #F5F1E8 100%%)"
        ) % (col, col, col)
        legend.append(
            '<div class="legend-item">'
            '<span class="legend-swatch" style="background:%s;"></span>'
            '<span class="legend-swatch" style="background:%s;background-size:6px 6px;border:1px solid #1B1B18;"></span>'
            '%s</div>' % (col, stripe_bg, p)
        )
    legend.append('</div>')
    legend.append('<div class="chart-note" style="margin-top:6px;">Solid bars = signups &middot; Striped bars = cancellations</div>')
    return "\n".join(out) + "\n" + "\n".join(legend)


def render_weekly_svg(weekly, plans):
    W, H = 880, 360
    ml, mr, mt, mb = 50, 20, 20, 60
    cw, ch = W - ml - mr, H - mt - mb
    n = len(weekly)
    max_val = max((w["total"] for w in weekly), default=1)

    def nice_max(v):
        if v <= 5: return 5
        if v <= 10: return 10
        if v <= 15: return 15
        if v <= 20: return 20
        return ((v // 5) + 1) * 5

    y_max = nice_max(max_val)
    bar_w = cw / n * 0.7
    gap = cw / n - bar_w

    out = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit;">' % (W, H)]
    for i in range(5):
        yv = y_max * (4 - i) / 4
        y = mt + ch * i / 4
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#E8E3D8" stroke-width="1"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="#7A7466">%d</text>' % (ml - 8, y + 4, int(round(yv))))
    for i, wk in enumerate(weekly):
        x = ml + i * (cw / n) + gap / 2
        y_cursor = mt + ch
        for pi, p in enumerate(plans):
            v = wk["counts"].get(p, 0)
            if v <= 0:
                continue
            h = ch * v / y_max
            y_cursor -= h
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"><title>%s - %s: %d</title></rect>' %
                       (x, y_cursor, bar_w, h, PALETTE[pi % len(PALETTE)], wk["label"], p, v))
        if wk["total"] > 0:
            ty = mt + ch - (ch * wk["total"] / y_max)
            out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="600" fill="#1B1B18">%d</text>' %
                       (x + bar_w / 2, ty - 5, wk["total"]))
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" fill="#7A7466" transform="rotate(-35 %.1f %.1f)">%s</text>' %
                   (x + bar_w / 2, H - mb + 18, x + bar_w / 2, H - mb + 18, wk["label"]))
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#1B1B18" stroke-width="1"/>' % (ml, H - mb, W - mr, H - mb))
    out.append('</svg>')
    return "\n".join(out)


def render_weekly_cancellations_svg(weekly_cx, plans_order):
    """Same layout as render_weekly_svg, but uses orange-family colors so bars read as cancellations."""
    W, H = 880, 360
    ml, mr, mt, mb = 50, 20, 20, 60
    cw, ch = W - ml - mr, H - mt - mb
    n = len(weekly_cx)
    max_val = max((w["total"] for w in weekly_cx), default=1)

    def nice_max(v):
        if v <= 5: return 5
        if v <= 10: return 10
        if v <= 15: return 15
        if v <= 20: return 20
        return ((v // 5) + 1) * 5

    y_max = nice_max(max_val)
    bar_w = cw / n * 0.7
    gap = cw / n - bar_w

    # Orange-family palette so cancellations read as "outflow" vs signups
    CX_PALETTE = ["#C44819", "#E07547", "#B85C2E", "#A2431F", "#F0A17C", "#7A7466", "#4A4844", "#D4D0C4"]

    out = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit;">' % (W, H)]
    for i in range(5):
        yv = y_max * (4 - i) / 4
        y = mt + ch * i / 4
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#E8E3D8" stroke-width="1"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="#7A7466">%d</text>' % (ml - 8, y + 4, int(round(yv))))
    for i, wk in enumerate(weekly_cx):
        x = ml + i * (cw / n) + gap / 2
        y_cursor = mt + ch
        for pi, p in enumerate(plans_order):
            v = wk["counts"].get(p, 0)
            if v <= 0:
                continue
            h = ch * v / y_max
            y_cursor -= h
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"><title>%s - %s: %d</title></rect>' %
                       (x, y_cursor, bar_w, h, CX_PALETTE[pi % len(CX_PALETTE)], wk["label"], p, v))
        if wk["total"] > 0:
            ty = mt + ch - (ch * wk["total"] / y_max)
            out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" font-weight="600" fill="#C44819">%d</text>' %
                       (x + bar_w / 2, ty - 5, wk["total"]))
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" fill="#7A7466" transform="rotate(-35 %.1f %.1f)">%s</text>' %
                   (x + bar_w / 2, H - mb + 18, x + bar_w / 2, H - mb + 18, wk["label"]))
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#1B1B18" stroke-width="1"/>' % (ml, H - mb, W - mr, H - mb))
    out.append('</svg>')

    # Legend uses the same CX palette
    legend = ['<div class="legend">']
    for i, p in enumerate(plans_order):
        legend.append('<div class="legend-item"><span class="legend-swatch" style="background:%s;"></span>%s</div>' %
                      (CX_PALETTE[i % len(CX_PALETTE)], p))
    legend.append('</div>')
    return "\n".join(out) + "\n" + "\n".join(legend)


def aggregate_weekly_cancellations(cancellations, today, weeks, plans):
    """
    Bucket cancellations by End Date into Friday-anchored weeks matching the signup chart.
    Cancellations with a plan not in `plans` fall into "Other".
    Returns a list of {label, counts, total} — one entry per week (oldest first).
    """
    this_week_monday = week_start(today)
    week_mondays = [this_week_monday - datetime.timedelta(days=(weeks - 1 - i) * 7) for i in range(weeks)]
    buckets = {wm: {p: 0 for p in plans} for wm in week_mondays}
    other_seen = False
    for c in cancellations:
        d = safe_date(c["end_date"])
        if not d:
            continue
        wm = week_start(d)
        if wm not in buckets:
            continue
        plan = c["plan"]
        if plan in buckets[wm]:
            buckets[wm][plan] += 1
        else:
            other_seen = True
            buckets[wm].setdefault("Other", 0)
            buckets[wm]["Other"] += 1
    if other_seen:
        for wm in week_mondays:
            buckets[wm].setdefault("Other", 0)
    return [{
        "label": wm.strftime("%b %d"),
        "counts": buckets[wm],
        "total": sum(buckets[wm].values()),
    } for wm in week_mondays]


def render_ytd_svg(ytd, plans):
    W, H = 880, 400
    ml, mr, mt, mb = 50, 20, 20, 60
    cw, ch = W - ml - mr, H - mt - mb
    n = len(ytd)
    max_val = max((w["cum_total"] for w in ytd), default=1)
    y_max = ((max_val // 50) + 1) * 50
    bar_w = cw / n * 0.75
    gap = cw / n - bar_w

    out = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit;">' % (W, H)]
    steps = 5
    for i in range(steps + 1):
        yv = y_max * (steps - i) / steps
        y = mt + ch * i / steps
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#E8E3D8" stroke-width="1"/>' % (ml, y, W - mr, y))
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" fill="#7A7466">%d</text>' % (ml - 8, y + 4, int(round(yv))))
    for i, wk in enumerate(ytd):
        x = ml + i * (cw / n) + gap / 2
        y_cursor = mt + ch
        for pi, p in enumerate(plans):
            v = wk["cum_counts"].get(p, 0)
            if v <= 0:
                continue
            h = ch * v / y_max
            y_cursor -= h
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"><title>Week of %s - %s: %d (cumulative)</title></rect>' %
                       (x, y_cursor, bar_w, h, plan_color(p, pi), wk["label"], p, v))
            if h >= 14:
                out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10" font-weight="600" fill="#F5F1E8" stroke="#1B1B18" stroke-width="0.4" paint-order="stroke">%d</text>' %
                           (x + bar_w / 2, y_cursor + h / 2 + 3, v))
        if wk["cum_total"] > 0:
            ty = mt + ch - (ch * wk["cum_total"] / y_max)
            out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="10" font-weight="600" fill="#1B1B18">%d</text>' %
                       (x + bar_w / 2, ty - 5, wk["cum_total"]))
        out.append('<text x="%.1f" y="%.1f" text-anchor="middle" font-size="11" fill="#7A7466" transform="rotate(-45 %.1f %.1f)">%s</text>' %
                   (x + bar_w / 2, H - mb + 18, x + bar_w / 2, H - mb + 18, wk["label"]))
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#1B1B18" stroke-width="1"/>' % (ml, H - mb, W - mr, H - mb))
    out.append('</svg>')
    return "\n".join(out)


def render_plan_svg(plan_counts, plans):
    items = [(p, v) for p, v in sorted(plan_counts.items(), key=lambda x: -x[1]) if v > 0]
    if not items:
        return '<div style="color:#7A7466;padding:20px;">No active members.</div>'
    row_h = 36
    label_w = 280
    bar_max_w = 480
    W = label_w + bar_max_w + 80
    H = len(items) * row_h + 20
    max_val = max(v for _, v in items)
    total = sum(v for _, v in items)
    out = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" style="width:100%%;height:auto;font-family:inherit;">' % (W, H)]
    for i, (plan, v) in enumerate(items):
        y = 10 + i * row_h
        out.append('<text x="%d" y="%.1f" text-anchor="end" font-size="13" fill="#1B1B18">%s</text>' % (label_w - 8, y + row_h / 2 + 4, plan))
        bw = bar_max_w * v / max_val
        color = plan_color(plan, plans.index(plan) if plan in plans else 0)
        out.append('<rect x="%d" y="%.1f" width="%.1f" height="%d" fill="%s"/>' % (label_w, y + 6, bw, row_h - 12, color))
        pct = v / total * 100
        out.append('<text x="%.1f" y="%.1f" font-size="12" font-weight="600" fill="#1B1B18">%d (%.1f%%)</text>' %
                   (label_w + bw + 6, y + row_h / 2 + 4, v, pct))
    out.append('</svg>')
    return "\n".join(out)


def render_legend(plans):
    parts = ['<div class="legend">']
    for i, p in enumerate(plans):
        parts.append('<div class="legend-item"><span class="legend-swatch" style="background:%s;"></span>%s</div>' %
                     (plan_color(p, i), p))
    parts.append('</div>')
    return "\n".join(parts)


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_weekly_cancellations_section(weekly_cx, plans_order, has_data):
    """Section C — the between-B-and-C chart. Falls back to a note when no Combined File is available."""
    if not has_data:
        return ('<div class="section-header"><div class="section-badge">C</div>'
                '<div class="section-title">Weekly cancellations</div>'
                '<div class="section-rule"></div></div>'
                '<div class="chart-block"><div class="chart-note">'
                'No Combined File was provided, so cancellation history could not be loaded. '
                'Re-run with <code>--combined-xlsx</code> to enable this chart.'
                '</div></div>')
    total = sum(w["total"] for w in weekly_cx)
    n_weeks = len(weekly_cx)
    svg = render_weekly_cancellations_svg(weekly_cx, plans_order)
    return (
        '<div class="section-header"><div class="section-badge">C</div>'
        '<div class="section-title">Weekly cancellations</div>'
        '<div class="section-rule"></div></div>'
        '<div class="chart-block">'
        '<div class="chart-title">Last %d weeks &middot; stacked by plan</div>'
        '<div class="chart-note">Cancellations from the Combined File\'s Name Tracking tab, '
        'bucketed by End Date into the same Friday &rarr; Thursday weeks as chart B. '
        'Total in window: <strong>%d</strong>.</div>'
        '%s'
        '</div>'
    ) % (n_weeks, total, svg)


def render_cancellations_section(diff):
    if diff is None:
        return ""
    cancelled = diff["cancelled_this_run"]
    added = diff["new_this_run"]
    parts = []
    parts.append('<div class="section-header"><div class="section-badge">F</div>'
                 '<div class="section-title">Cancellations this run</div>'
                 '<div class="section-rule"></div></div>')
    parts.append('<div class="chart-block">')
    parts.append('<div class="chart-title">Diff vs. prior Name Tracking snapshot</div>')
    parts.append('<div class="chart-note">Pull date <strong>%s</strong>. New snapshot tab: <strong>%s</strong>. '
                 'Every previously-active member whose (name + plan) is missing or non-Active in this CSV is stamped Cancelled with End Date = pull date. '
                 'New name+plan pairs not previously tracked are appended as Active.</div>' %
                 (_esc(diff["pull_date"]), _esc(diff["new_tab_name"])))
    parts.append('<div class="diff-cards">')
    parts.append('<div class="card"><div class="card-label">Prior active</div>'
                 '<div class="card-number">%d</div>'
                 '<div class="card-sub">In Name Tracking before run</div></div>' % diff["prior_active_count"])
    parts.append('<div class="card"><div class="card-label">Current active</div>'
                 '<div class="card-number">%d</div>'
                 '<div class="card-sub">In new CSV snapshot</div></div>' % diff["current_active_count"])
    parts.append('<div class="card"><div class="card-label">Cancelled this run</div>'
                 '<div class="card-number orange">%d</div>'
                 '<div class="card-sub">Marked Cancelled today</div></div>' % len(cancelled))
    parts.append('<div class="card"><div class="card-label">New this run</div>'
                 '<div class="card-number">%d</div>'
                 '<div class="card-sub">Newly tracked</div></div>' % len(added))
    parts.append('</div>')

    if cancelled:
        parts.append('<div class="diff-list-title">Cancelled <span class="badge-lost">(%d)</span></div>' % len(cancelled))
        parts.append('<table class="diff-table"><thead><tr>'
                     '<th>Name</th><th>Plan</th><th>Start date</th><th>End date</th><th>First seen</th></tr></thead><tbody>')
        for r in cancelled:
            parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                         (_esc(r["display_name"]), _esc(r["plan"]), _esc(r["start_date"]),
                          _esc(r["end_date"]), _esc(r["first_seen"])))
        parts.append('</tbody></table>')
    else:
        parts.append('<div class="chart-note">No cancellations this run &mdash; every previously-active member is still Active.</div>')

    if added:
        parts.append('<div class="diff-list-title">Newly tracked <span class="badge-new">(%d)</span></div>' % len(added))
        parts.append('<table class="diff-table"><thead><tr>'
                     '<th>Name</th><th>Plan</th><th>Start date</th><th>First seen</th></tr></thead><tbody>')
        for r in added:
            parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' %
                         (_esc(r["display_name"]), _esc(r["plan"]), _esc(r["start_date"]), _esc(r["first_seen"])))
        parts.append('</tbody></table>')

    parts.append('</div>')  # chart-block
    return "\n".join(parts)




def render_html(agg, source_file, template_path, diff=None, excluded_plans=None,
                weekly_cancellations=None, nt_cumulative=None):
    plans = agg["plans"]
    weekly = agg["weekly"]
    ytd = agg["ytd_weekly_cum"]
    summary = agg["summary"]

    last_signups_total = sum(w["total"] for w in weekly)
    current_week_label = weekly[-1]["label"] if weekly else "-"
    n_weeks = len(weekly)
    avg_per_week = (last_signups_total / n_weeks) if n_weeks else 0.0

    try:
        rep_d = datetime.date.fromisoformat(summary["report_generated"])
        pretty_date = rep_d.strftime("%A, %B %d, %Y")
        parts = pretty_date.split(",")
        if len(parts) >= 2 and " 0" in parts[1]:
            parts[1] = parts[1].replace(" 0", " ", 1)
            pretty_date = ",".join(parts)
    except Exception:
        pretty_date = summary["report_generated"]

    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    repl = {
        "__PRETTY_DATE__": pretty_date,
        "__SOURCE_FILE__": source_file,
        "__TOTAL_RECORDS__": str(summary["total_records"]),
        "__TOTAL_ACTIVE__": str(summary["total_active"]),
        "__LAST_SIGNUPS_TOTAL__": str(last_signups_total),
        "__PLAN_COUNT__": str(len(plans)),
        "__AVG_PER_WEEK__": "%.1f" % avg_per_week,
        "__N_WEEKS__": str(n_weeks),
        "__CURRENT_WEEK__": current_week_label,
        "__WEEKLY_SVG__": render_weekly_diverging_svg(weekly, weekly_cancellations, plans),
        "__NET_SVG__": render_net_svg(weekly, weekly_cancellations),
        "__YTD_SVG__": render_ytd_svg(ytd, plans),
        "__PLAN_SVG__": render_plan_svg(summary["plan_breakdown"], plans),
        "__LEGEND__": render_legend(plans),
        "__REPORT_GENERATED__": summary["report_generated"],
        "__CANCELLATIONS_SECTION__": render_nt_cumulative_section(
            nt_cumulative or [], plans, nt_cumulative is not None),
        "__WEEKLY_CANCELLATIONS_SECTION__": "",
        "__EXCLUDED_PLANS__": ", ".join(sorted(excluded_plans)) if excluded_plans else "none",
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--weeks", type=int, default=13)
    ap.add_argument("--include-status", default="Active")
    ap.add_argument("--plans", default=None)
    ap.add_argument("--exclude-plans", default=",".join(sorted(DEFAULT_EXCLUDED_PLANS)))
    ap.add_argument("--today", default=None)
    ap.add_argument("--template", default=DEFAULT_TEMPLATE)
    ap.add_argument("--combined-xlsx", default=None)
    ap.add_argument("--pull-date", default=None)
    ap.add_argument("--updated-xlsx", default=None)
    args = ap.parse_args()

    today = datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()
    allowed_statuses = {s.strip() for s in args.include_status.split(",") if s.strip()}
    allowed_plans = {p.strip() for p in args.plans.split(",")} if args.plans else None
    excluded_plans = {p.strip() for p in args.exclude_plans.split(",") if p.strip()}

    all_rows = parse_csv(args.csv)
    rows = filter_rows(all_rows, allowed_statuses, allowed_plans, excluded_plans=excluded_plans)

    diff = None
    if args.combined_xlsx:
        pull_date = extract_pull_date(args.csv, args.pull_date)
        updated_path = args.updated_xlsx or os.path.join(
            os.path.dirname(os.path.abspath(args.output)) or ".",
            "combined_file_updated.xlsx"
        )
        diff = update_combined_file(
            args.combined_xlsx, all_rows, pull_date, updated_path,
            excluded_plans=excluded_plans,
        )

    agg = aggregate(rows, today, args.weeks)

    weekly_cx = None
    nt_cumulative = None
    if args.combined_xlsx:
        cancellations = read_cancellations(args.combined_xlsx, excluded_plans=excluded_plans)
        weekly_cx = aggregate_weekly_cancellations(cancellations, today, args.weeks, agg["plans"])
        nt_rows = read_all_name_tracking(args.combined_xlsx, excluded_plans=excluded_plans)
        nt_cumulative = aggregate_nt_cumulative(nt_rows, today, args.weeks, agg["plans"])

    html = render_html(agg, os.path.basename(args.csv), args.template,
                       diff=diff, excluded_plans=excluded_plans,
                       weekly_cancellations=weekly_cx, nt_cumulative=nt_cumulative)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    s = agg["summary"]
    print("Wrote %s (%d bytes)" % (args.output, len(html)))
    print("Active: %d, Plans: %d" % (s["total_active"], len(agg["plans"])))
    if weekly_cx is not None:
        total_up = sum(w["total"] for w in agg["weekly"])
        total_dn = sum(w["total"] for w in weekly_cx)
        print("Signups=%d, Cancellations=%d, Net=%+d" % (total_up, total_dn, total_up - total_dn))
        print("NT cumulative: %d -> %d" % (nt_cumulative[0]["cum_total"], nt_cumulative[-1]["cum_total"]))


if __name__ == "__main__":
    sys.exit(main())
