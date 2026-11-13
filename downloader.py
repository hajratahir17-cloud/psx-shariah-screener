"""
Downloads each company's annual/interim report PDF listed in
company_list.csv, with full validation so nothing fails silently.

Usage:
    python downloader.py

Reads company_list.csv (columns: Ticker, Company Name, Market Price,
Link - the real columns in this project's CSV) and saves each PDF to
./annual_reports/<TICKER>.pdf, writing download_report.csv with the
outcome of every row.
"""

import os
import csv
import time
import pdfplumber
import requests

COMPANY_LIST_CSV = "company_list.csv"
PDF_FOLDER = "./annual_reports"
DOWNLOAD_REPORT_CSV = "./download_report.csv"

MAX_RETRIES = 3
TIMEOUT_SECONDS = 90  # dps.psx.com.pk can be very slow (confirmed: a 164KB
                       # file took 135s in testing) - a short timeout here
                       # just means every download fails with a false alarm
DELAY_BETWEEN_REQUESTS = 1.5  # be polite - don't hammer the server
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def download_pdf(url, dest_path):
    """Download a PDF with retries, backoff, and content validation.
    Returns (success: bool, message: str)."""
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS, stream=True)
        except requests.exceptions.Timeout:
            if attempt == MAX_RETRIES:
                return False, f"Timed out after {MAX_RETRIES} attempts"
            time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError as e:
            if attempt == MAX_RETRIES:
                return False, f"Connection error: {e}"
            time.sleep(2 ** attempt)
            continue
        except requests.exceptions.RequestException as e:
            return False, f"Request failed: {e}"

        if resp.status_code == 404:
            return False, "404 - link is broken/expired (document IDs on PSX can rotate)"
        if resp.status_code == 403:
            return False, "403 - access denied (may need to download this one manually)"
        if resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                return False, f"Server error {resp.status_code} after {MAX_RETRIES} attempts"
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            return False, f"Unexpected HTTP status {resp.status_code}"

        # stream=True means the headers above arrive fast but the body
        # isn't actually read until .content is accessed - a slow/stalled
        # server can time out HERE, after the connection already looked
        # successful, so this needs its own retry handling too.
        try:
            content = resp.content
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES:
                return False, f"Connection dropped while downloading body: {e}"
            time.sleep(2 ** attempt)
            continue

        # Guard: some broken links return a small HTML "not found" page
        # with a 200 status instead of a proper 404 - check magic bytes.
        if not content.startswith(b"%PDF"):
            return False, "URL did not return a real PDF (likely an HTML error page or login wall)"

        if len(content) < 1024:
            return False, f"Downloaded file is suspiciously small ({len(content)} bytes) - likely corrupted"

        with open(dest_path, "wb") as f:
            f.write(content)

        try:
            with pdfplumber.open(dest_path) as pdf:
                if len(pdf.pages) == 0:
                    return False, "PDF has zero pages"
        except Exception as e:
            os.remove(dest_path)
            return False, f"Downloaded file is not a valid/openable PDF: {e}"

        return True, f"OK ({len(content)/1024:.0f} KB, attempt {attempt})"

    return False, "Exhausted retries"


def load_company_list():
    if not os.path.exists(COMPANY_LIST_CSV):
        print(f"ERROR: {COMPANY_LIST_CSV} not found.")
        return []

    companies = []
    seen_tickers = set()
    with open(COMPANY_LIST_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            ticker = (row.get("Ticker") or "").strip()
            url = (row.get("Link") or "").strip()
            if not ticker or not url or not url.startswith("http"):
                print(f"  ! Row {i}: missing ticker or usable URL, skipping")
                continue
            if ticker in seen_tickers:
                print(f"  ! Row {i}: duplicate ticker '{ticker}', skipping")
                continue
            seen_tickers.add(ticker)
            companies.append({"ticker": ticker, "name": (row.get("Company Name") or "").strip(), "url": url})
    return companies


def run():
    os.makedirs(PDF_FOLDER, exist_ok=True)
    companies = load_company_list()
    if not companies:
        return

    report_rows = []
    for co in companies:
        dest = os.path.join(PDF_FOLDER, f"{co['ticker']}.pdf")
        print(f"[{co['ticker']}] Downloading...")
        ok, msg = download_pdf(co["url"], dest)
        print(f"  {msg}")
        report_rows.append({"ticker": co["ticker"], "status": "OK" if ok else "FAILED", "detail": msg})
        time.sleep(DELAY_BETWEEN_REQUESTS)

    with open(DOWNLOAD_REPORT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "status", "detail"])
        writer.writeheader()
        writer.writerows(report_rows)

    ok_count = sum(1 for r in report_rows if r["status"] == "OK")
    print(f"\nDone. {ok_count}/{len(companies)} downloaded successfully.")
    print(f"See {DOWNLOAD_REPORT_CSV} for the full breakdown.")


if __name__ == "__main__":
    run()
