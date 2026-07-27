#!/usr/bin/env python3
"""
GitHub Action worker for the Bedrock KPI 4 pipeline.

Logs into HouseCall Pro with Playwright, opens the Customer Plans / Service
Agreements summary, and scrapes ALL pages of the visible table into a CSV
that gets uploaded to the Drive folder given by DRIVE_CSV_FOLDER_ID.
"""
from __future__ import annotations

import csv
import datetime
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drive_client import get_service, upload_file  # noqa: E402


HCP_LOGIN_URL = "https://pro.housecallpro.com/app"
HCP_SUMMARY_URL = "https://pro.housecallpro.com/app/service_agreements/summary"

CSV_HEADERS = [
    "Plan", "Start Date", "End Date", "Status",
    "First Name", "Last Name", "Display Name",
    "Mobile Number", "Home Number", "Email", "Company", "ID",
    "Address Street Line 1", "Address Street Line 2",
    "Address City", "Address State", "Address Postal Code",
    "Address Billing?", "Address Notes",
]


def require_env(name):
    v = os.environ.get(name)
    if not v:
        raise SystemExit("Missing required env var: %s" % name)
    return v


def _first_visible(page, selectors, timeout=10_000):
    last_err = None
    for sel in selectors:
        try:
            loc = sel if hasattr(sel, "fill") else page.locator(sel)
            loc.first.wait_for(state="visible", timeout=timeout)
            return loc.first
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("no selector matched")


def login(page, email, password):
    print("Logging into HouseCall Pro...")
    page.goto(HCP_LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15_000)
    print("Landed at URL: %s" % page.url)
    print("Page title: %s" % page.title())

    for label in ["Accept all", "Accept cookies", "Got it", "I agree", "OK"]:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).click(timeout=2_000)
            print("Dismissed banner: %s" % label)
            break
        except Exception:
            pass

    inputs = page.locator("input:visible")
    inputs.first.wait_for(state="visible", timeout=15_000)
    n_inputs = inputs.count()
    print("Visible inputs on login page: %d" % n_inputs)

    if n_inputs < 2:
        raise SystemExit("Expected at least 2 input fields on the login page.")

    email_field = None
    password_field = None
    typed_email = page.locator('input[type="email"]:visible')
    typed_pwd = page.locator('input[type="password"]:visible')
    if typed_email.count() > 0:
        email_field = typed_email.first
    if typed_pwd.count() > 0:
        password_field = typed_pwd.first
    if email_field is None:
        by_id = page.locator('input#email:visible')
        email_field = by_id.first if by_id.count() > 0 else inputs.nth(0)
    if password_field is None:
        by_id = page.locator('input#password:visible')
        password_field = by_id.first if by_id.count() > 0 else inputs.nth(1)

    email_field.fill(email)
    password_field.fill(password)

    submit = _first_visible(page, [
        page.get_by_role("button", name=re.compile(r"^\s*(sign in|log in|continue)\s*$", re.I)),
        page.locator('button:has-text("Sign in")'),
        page.locator('button:has-text("Log in")'),
        'button[type="submit"]',
        'input[type="submit"]',
    ], timeout=5_000)
    submit.click()

    try:
        page.wait_for_url(
            re.compile(r"pro\.housecallpro\.com/app/(?!log_in|login|sign_in)"),
            timeout=60_000,
        )
    except Exception:
        print("URL still on login-adjacent page: %s" % page.url)
        raise
    print("Logged in. Now at: %s" % page.url)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    print("Post-idle URL: %s" % page.url)


def open_service_agreements(page):
    print("Opening Customer Plans summary...")
    page.goto(HCP_SUMMARY_URL, wait_until="networkidle", timeout=60_000)
    print("Service agreements URL: %s" % page.url)
    print("Service agreements title: %s" % page.title())
    tried = [
        "text=/\\d+\\s+customer plans/i",
        "text=/customer plans/i",
        "text=/service agreements/i",
        "table",
    ]
    for sel in tried:
        try:
            page.wait_for_selector(sel, timeout=10_000)
            print("Matched selector: %s" % sel)
            break
        except Exception:
            continue
    time.sleep(3)


def try_set_max_page_size(page):
    """Try to set the rows-per-page selector to its largest value (usually 100)."""
    candidates = [
        "select[name*='page' i]",
        "select[aria-label*='rows per page' i]",
        "select[aria-label*='per page' i]",
        ".MuiTablePagination-select",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() == 0:
            continue
        try:
            options = loc.first.locator("option").all_inner_texts()
            print("Found page-size selector %s with options: %s" % (sel, options))
            # pick the numerically largest option
            nums = []
            for t in options:
                m = re.match(r"\s*(\d+)", t)
                if m:
                    nums.append(int(m.group(1)))
            if not nums:
                continue
            biggest = max(nums)
            loc.first.select_option(str(biggest))
            print("Set page size to %d" % biggest)
            time.sleep(2)
            return True
        except Exception as e:
            print("Page-size selector %s attempt failed: %s" % (sel, e))
    # MUI sometimes uses a button-based select
    try:
        btn = page.locator("[aria-labelledby*='per-page' i], [aria-label*='rows per page' i]")
        if btn.count() > 0:
            btn.first.click(timeout=3_000)
            time.sleep(0.5)
            # Look for the biggest number in the popup
            opts = page.locator("[role='option']").all_inner_texts()
            print("Popup rows-per-page options: %s" % opts)
            nums = [(int(re.match(r"\s*(\d+)", t).group(1)), t) for t in opts if re.match(r"\s*\d+", t)]
            if nums:
                biggest_text = max(nums)[1]
                page.locator("[role='option']", has_text=biggest_text.strip()).first.click()
                print("Selected page size option: %s" % biggest_text)
                time.sleep(2)
                return True
    except Exception as e:
        print("Button-select page-size attempt failed: %s" % e)
    return False


def find_row_selector(page):
    for sel in [
        "tbody tr",
        "[role='row']",
        "tr[data-row-id]",
        "[data-row-id]",
        ".MuiDataGrid-row",
    ]:
        try:
            c = page.locator(sel).count()
            if c >= 2:
                return sel, c
        except Exception:
            pass
    return None, 0


def find_cell_selector(row):
    for cs in ["td", "[role='cell']", "[role='gridcell']", "[data-field]"]:
        try:
            if row.locator(cs).count() >= 4:
                return cs
        except Exception:
            pass
    return None


def _to_iso(s):
    s = (s or "").strip()
    if not s:
        return ""
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def parse_row(cells):
    n = cells.count()
    if n < 4:
        return None
    try:
        display_name = cells.nth(0).inner_text().strip()
        phone = cells.nth(1).inner_text().strip()
        plan = cells.nth(2).inner_text().strip()
        address_lines = cells.nth(3).inner_text().split("\n")
        start = cells.nth(4).inner_text().strip() if n > 4 else ""
        end = cells.nth(5).inner_text().strip() if n > 5 else ""
        status = cells.nth(7).inner_text().strip() if n > 7 else ""
    except Exception:
        return None
    if not display_name or display_name.lower().startswith("customer"):
        return None
    street = address_lines[0].strip() if address_lines else ""
    city_state_zip = address_lines[1].strip() if len(address_lines) > 1 else ""
    m = re.match(r"(.+?),\s*([A-Z]{2})\s*(\d{5})?", city_state_zip)
    city, state, zipc = ("", "", "")
    if m:
        city, state, zipc = m.group(1), m.group(2), (m.group(3) or "")
    return {
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
    }


def scrape_current_page(page, row_sel, cell_sel):
    rows_loc = page.locator(row_sel)
    total = rows_loc.count()
    scraped = []
    for i in range(total):
        r = parse_row(rows_loc.nth(i).locator(cell_sel))
        if r:
            scraped.append(r)
    return scraped


def click_next_page(page):
    """Click a 'Next page' button. Returns True on success."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"^\s*next\s*(page)?\s*$", re.I)),
        page.locator("button[aria-label*='next page' i]"),
        page.locator("[aria-label*='Go to next page' i]"),
        page.locator("button:has-text('Next')"),
    ]
    for c in candidates:
        try:
            if c.count() == 0:
                continue
            btn = c.first
            if not btn.is_enabled():
                return False
            btn.click(timeout=3_000)
            time.sleep(2)
            return True
        except Exception:
            continue
    return False


def scrape_all_rows(page):
    print("Scraping Customer Plans table (all pages)...")
    try:
        page.screenshot(path="before_scrape.png", full_page=True)
    except Exception:
        pass

    try_set_max_page_size(page)

    row_sel, count = find_row_selector(page)
    if not row_sel:
        print("No row selector matched")
        return []
    print("Row selector: %s (initial %d)" % (row_sel, count))

    first_row = page.locator(row_sel).nth(0)
    cell_sel = find_cell_selector(first_row)
    if not cell_sel:
        # try row 1 in case row 0 is a header
        cell_sel = find_cell_selector(page.locator(row_sel).nth(1))
    print("Cell selector: %s" % cell_sel)
    if not cell_sel:
        return []

    all_rows = []
    seen_keys = set()
    max_pages = 60  # 60 * 100 = 6000, plenty of headroom
    for page_num in range(max_pages):
        page_rows = scrape_current_page(page, row_sel, cell_sel)
        # dedupe defensively — some UIs re-render current page after page-size change
        new_this_page = 0
        for r in page_rows:
            key = (r["Display Name"], r["Plan"], r["Start Date"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_rows.append(r)
            new_this_page += 1
        print("Page %d: %d rows (%d new). Total so far: %d"
              % (page_num + 1, len(page_rows), new_this_page, len(all_rows)))
        if new_this_page == 0:
            # No new rows — pagination didn't advance
            break
        if not click_next_page(page):
            print("No more pages.")
            break
    print("Scraped %d total rows across all pages." % len(all_rows))
    return all_rows


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("Wrote %s (%d rows)" % (path, len(rows)))


def main():
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
            rows = scrape_all_rows(page)
            if not rows:
                page.screenshot(path="failed_scrape.png", full_page=True)
                raise SystemExit("No rows scraped.")
            write_csv(rows, csv_path)
        except Exception:
            try:
                page.screenshot(path="failure.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()

    service = get_service()
    upload_file(service, csv_path, folder_id, mime_type="text/csv",
                supports_all_drives=True)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
