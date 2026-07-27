#!/usr/bin/env python3
"""
GitHub Action worker for the Bedrock KPI 4 pipeline.

Logs into HouseCall Pro with Playwright, opens the Customer Plans / Service
Agreements summary, and scrapes the visible table into a CSV that gets
uploaded to the Drive folder given by DRIVE_CSV_FOLDER_ID.
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

    try:
        page.screenshot(path="login_page.png", full_page=True)
    except Exception:
        pass

    inputs = page.locator("input:visible")
    inputs.first.wait_for(state="visible", timeout=15_000)
    n_inputs = inputs.count()
    print("Visible inputs on login page: %d" % n_inputs)

    for i in range(min(n_inputs, 4)):
        try:
            attrs = inputs.nth(i).evaluate(
                "el => ({type: el.type, name: el.name, id: el.id, placeholder: el.placeholder})"
            )
            print("  input[%d]: %s" % (i, attrs))
        except Exception:
            pass

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

    # Wait for URL to leave the login/log_in interim page.
    try:
        page.wait_for_url(
            re.compile(r"pro\.housecallpro\.com/app/(?!log_in|login|sign_in)"),
            timeout=60_000,
        )
    except Exception:
        print("URL still on login-adjacent page: %s" % page.url)
        try:
            page.screenshot(path="post_login_stuck.png", full_page=True)
            print("Saved post_login_stuck.png for diagnosis")
        except Exception:
            pass
        try:
            body_snippet = page.locator("body").inner_text()[:2000]
            print("Body text (first 2000 chars):\n%s" % body_snippet)
        except Exception:
            pass
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
        "[role='row']",
        "table",
    ]
    ok = False
    for sel in tried:
        try:
            page.wait_for_selector(sel, timeout=10_000)
            print("Matched selector: %s" % sel)
            ok = True
            break
        except Exception:
            continue
    if not ok:
        try:
            page.screenshot(path="service_agreements_page.png", full_page=True)
            print("Saved service_agreements_page.png for diagnosis")
            body_snippet = page.locator("body").inner_text()[:2000]
            print("Body text (first 2000 chars):\n%s" % body_snippet)
        except Exception:
            pass
        raise RuntimeError("Could not find Customer Plans table on the page.")


def trigger_email_export(page):
    try:
        candidates = [
            page.locator("[aria-label*='download' i]"),
            page.locator("button:has(svg)").filter(has_text=""),
            page.locator("[data-testid*='download' i]"),
        ]
        for loc in candidates:
            if loc.count() > 0:
                loc.first.click(timeout=5_000)
                break
        page.get_by_role("button", name=re.compile(r"^send$", re.I)).click(timeout=5_000)
        print("Triggered email export.")
    except PWTimeout:
        print("Email export button not found (harmless).")
    except Exception as e:
        print("Email export skipped: %s" % e)


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


def scrape_all_rows(page):
    print("Scraping Customer Plans table...")
    prev_count = -1
    for _ in range(40):
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
        try:
            display_name = cells.nth(0).inner_text().strip()
            phone = cells.nth(1).inner_text().strip()
            plan = cells.nth(2).inner_text().strip()
            address_lines = cells.nth(3).inner_text().split("\n")
            start = cells.nth(4).inner_text().strip()
            end = cells.nth(5).inner_text().strip()
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
            trigger_email_export(page)
            rows = scrape_all_rows(page)
            if not rows:
                page.screenshot(path="failed_scrape.png", full_page=True)
                raise SystemExit("No rows scraped. See failed_scrape.png artifact.")
            write_csv(rows, csv_path)
        except Exception:
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
