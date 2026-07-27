#!/usr/bin/env python3
"""
GitHub Action worker.

Logs into HouseCall Pro with Playwright, opens the Customer Plans / Service
Agreements summary, triggers the "download" flow (which emails the CSV to the
account), then falls back to scraping the visible table into a CSV so we don't
have to wait for the email.

The scraped CSV is uploaded to the Drive folder given by DRIVE_CSV_FOLDER_ID,
where the Render cron job (run_kpi4.py) picks it up two hours later.

Env vars required:
  HCP_EMAIL              HouseCall Pro login email
  HCP_PASSWORD           HouseCall Pro password
  GOOGLE_SERVICE_ACCOUNT JSON service-account key
  DRIVE_CSV_FOLDER_ID    Target Drive folder ID

Selectors are intentionally coarse (looking for text labels rather than opaque
class names) so this survives most HCP UI tweaks. If HCP restructures the page
we'll see it in the Actions logs as a timeout waiting for a specific label.
"""
from __future__ import annotations

import csv
import datetime
import io
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drive_client import get_service, upload_file  # noqa: E402


HCP_LOGIN_URL = "https://pro.housecallpro.com/pro/login"
HCP_SUMMARY_URL = "https://pro.housecallpro.com/app/service_agreements/summary"

CSV_HEADERS = [
    "Plan", "Start Date", "End Date", "Status",
    "First Name", "Last Name", "Display Name",
    "Mobile Number", "Home Number", "Email", "Company", "ID",
    "Address Street Line 1", "Address Street Line 2",
    "Address City", "Address State", "Address Postal Code",
    "Address Billing?", "Address Notes",
]


def require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise SystemExit("Missing required env var: %s" % name)
    return v


def _first_visible(page, selectors, timeout=10_000):
    """Try each selector; return the first one that resolves. Raises if all fail."""
    last_err = None
    for sel in selectors:
        try:
            loc = sel if hasattr(sel, "fill") else page.locator(sel)
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("no selector matched")


def login(page, email: str, password: str) -> None:
    print("Logging into HouseCall Pro...")
    page.goto(HCP_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15_000)

    # Take an early screenshot for debugging
    try:
        page.screenshot(path="login_page.png", full_page=True)
    except Exception:
        pass

    # Try multiple ways to find the email input
    email_field = _first_visible(page, [
        'input[type="email"]',
        'input[name="email"]',
        'input[name="user[email]"]',
        'input[id*="email" i]',
        'input[placeholder*="email" i]',
        page.get_by_label(re.compile(r"email", re.I)),
        page.get_by_placeholder(re.compile(r"email", re.I)),
        page.get_by_role("textbox", name=re.compile(r"email", re.I)),
    ])
    email_field.fill(email)

    password_field = _first_visible(page, [
        'input[type="password"]',
        'input[name="password"]',
        'input[name="user[password]"]',
        'input[id*="password" i]',
        page.get_by_label(re.compile(r"password", re.I)),
        page.get_by_placeholder(re.compile(r"password", re.I)),
    ])
    password_field.fill(password)

    submit = _first_visible(page, [
        page.get_by_role("button", name=re.compile(r"^(log in|sign in|continue)", re.I)),
        'button[type="submit"]',
        'input[type="submit"]',
    ], timeout=5_000)
    submit.click()

    # Wait for a signed-in signal — anything indicating we made it past the login
    page.wait_for_url(re.compile(r"pro\.housecallpro\.com/app|dashboard|home"), timeout=45_000)
    print("Logged in.")


def open_service_agreements(page) -> None:
    print("Opening Customer Plans summary...")
    page.goto(HCP_SUMMARY_URL, wait_until="networkidle", timeout=60_000)
    # Wait for the "N customer plans" heading to appear.
    page.wait_for_selector("text=/\\d+\\s+customer plans/i", timeout=30_000)


def trigger_email_export(page) -> None:
    """Best-effort: click the download icon and confirm 'Send'. If HCP changes
    the UI and we can't find it, we still fall through to scraping."""
    try:
        # The download icon has no accessible label historically; try common patterns.
        candidates = [
            page.locator("[aria-label*='download' i]"),
            page.locator("button:has(svg)").filter(has_text=""),  # icon buttons
            page.locator("[data-testid*='download' i]"),
        ]
        for loc in candidates:
            if loc.count() > 0:
                loc.first.click(timeout=5_000)
                break
        page.get_by_role("button", name=re.compile(r"^send$", re.I)).click(timeout=5_000)
        print("Triggered email export.")
    except PWTimeout:
        print("Email export button not found (harmless — proceeding with scrape).")
    except Exception as e:
        print("Email export skipped: %s" % e)


def scrape_all_rows(page) -> list[dict]:
    """
    Iterate through every row on the Customer Plans page and return dicts
    matching CSV_HEADERS as closely as we can from what's visible.
    Only the columns actually shown on the page will be populated; the rest are blank.
    """
    print("Scraping Customer Plans table...")
    # Scroll to the bottom to force lazy-load of remaining rows.
    prev_count = -1
    for _ in range(40):  # cap iterations
        count = page.locator("[data-row-id], tr[data-row-id], [role='row']").count()
        if count == prev_count:
            break
        prev_count = count
        page.mouse.wheel(0, 20_000)
        time.sleep(0.6)
    rows_locator = page.locator("[role='row']")
    total = rows_locator.count()
    print("Found %d row elements (incl. header)." % total)

    scraped = []
    for i in range(total):
        row = rows_locator.nth(i)
        cells = row.locator("[role='cell']")
        n = cells.count()
        if n < 4:
            continue
        # Column order matches HCP's Customer Plans page:
        # Customer | Phone | Plan | Address | Start | End | Next service | Status | Billing cycle
        try:
            display_name = cells.nth(0).inner_text().strip()
            phone = cells.nth(1).inner_text().strip()
            plan = cells.nth(2).inner_text().strip()
            address_lines = cells.nth(3).inner_text().split("\n")
            start = cells.nth(4).inner_text().strip()
            end = cells.nth(5).inner_text().strip()
            # cells.nth(6) is Next service - not in export
            status = cells.nth(7).inner_text().strip() if n > 7 else ""
        except Exception:
            continue
        if not display_name or display_name.lower().startswith("customer"):
            continue
        street = address_lines[0].strip() if address_lines else ""
        city_state_zip = address_lines[1].strip() if len(address_lines) > 1 else ""
        m = re.match(r"(.+?),\s*([A-Z]{2})\s*(\d{5})?", city_state_zip)
        city, state, zipc = ("", "", "")
        if m:
            city, state, zipc = m.group(1), m.group(2), (m.group(3) or "")
        scraped.append({
            "Plan": plan,
            "Start Date": _to_iso(start),
            "End Date": _to_iso(end),
            "Status": status,
            "First Name": "", "Last Name": "",
            "Display Name": display_name,
            "Mobile Number": phone, "Home Number": "",
            "Email": "", "Company": "", "ID": "",
            "Address Street Line 1": street, "Address Street Line 2": "",
            "Address City": city, "Address State": state, "Address Postal Code": zipc,
            "Address Billing?": "", "Address Notes": "",
        })
    print("Scraped %d rows." % len(scraped))
    return scraped


def _to_iso(s: str) -> str:
    """Convert HCP-style dates like 'Jul 18, 2026' or 'May 09, 2025' to YYYY-MM-DD."""
    s = (s or "").strip()
    if not s:
        return ""
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("Wrote %s (%d rows)" % (path, len(rows)))


def main() -> int:
    email = require_env("HCP_EMAIL")
    password = require_env("HCP_PASSWORD")
    folder_id = require_env("DRIVE_CSV_FOLDER_ID")

    pull_date = datetime.date.today().isoformat()
    csv_path = "BedrockPlumbingDrainCleaning_service_agreements_export_%s.csv" % pull_date

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        )
        page = context.new_page()
        try:
            login(page, email, password)
            open_service_agreements(page)
            trigger_email_export(page)
            rows = scrape_all_rows(page)
            if not rows:
                page.screenshot(path="failed_scrape.png", full_page=True)
                raise SystemExit("No rows scraped. See failed_scrape.png artifact.")
            write_csv(rows, csv_path)
        except Exception as e:
            # Always leave a screenshot behind for debugging in the Actions artifact
            try:
                page.screenshot(path="failure.png", full_page=True)
                print("Saved failure.png for debugging")
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()

    service = get_service()
    upload_file(service, csv_path, folder_id, mime_type="text/csv")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
