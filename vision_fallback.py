"""
Free vision-LLM fallback for fields the deterministic extractor
(extract_financials.py) couldn't find or is unsure about.

Uses Google Gemini's free tier (Google AI Studio) via its OpenAI-
compatible endpoint - no credit card required. Get a free key at:
  https://aistudio.google.com/apikey -> Create API key
Then set it as an environment variable before running:
  setx GEMINI_API_KEY "AQ...."   (Windows, new shells)
  $env:GEMINI_API_KEY = "AQ...." (current PowerShell session)

This is a FALLBACK, not a replacement: the deterministic extractor is
free, instant, and already correct most of the time (see README). This
module is only called for the specific fields a page's deterministic
pass returned None for, to keep API usage low.
"""

import os
import re
import json
import base64
import time
import requests

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODEL = "gemini-flash-lite-latest"

FIELD_DESCRIPTIONS = {
    "total_assets": "Total Assets (the balance sheet grand total on the assets side)",
    "total_liabilities": "Total Liabilities (or Total Equity and Liabilities minus total equity, if no separate total-liabilities line exists)",
    "long_term_debt": "Long-term interest-bearing debt / long-term financing / borrowings (non-current liabilities section)",
    "short_term_debt": "Short-term borrowings / running finance (current liabilities section)",
    "investments": "Short-term or long-term investments (non-operating investment line items)",
    "other_income": "Other income (profit & loss statement, non-core income)",
    "total_revenue": "Total revenue / turnover / net sales (top line of the profit & loss statement)",
    "property_plant_equipment": "Property, plant and equipment (non-current assets)",
    "stock_in_trade": "Stock-in-trade / inventories",
    "cash_and_bank": "Cash and bank balances / cash and cash equivalents",
}


def _build_prompt(fields_needed):
    lines = [FIELD_DESCRIPTIONS[f] for f in fields_needed if f in FIELD_DESCRIPTIONS]
    field_list = "\n".join(f"- {f}: {desc}" for f, desc in zip(fields_needed, lines))
    return f"""You are reading one page of a Pakistani company's financial
statement (balance sheet or profit & loss). Extract ONLY these fields,
using the CURRENT period's column (the most recent date, usually the
leftmost data column - ignore prior-year comparative columns):

{field_list}

Rules:
- Return numbers in full rupees as plain numbers (no commas, no currency
  symbols). If the statement says "Rupees in '000" or "in thousand",
  multiply the figure by 1000 before returning it. If "in million",
  multiply by 1,000,000.
- If a line is shown as "-" or is genuinely absent, use null.
- If you cannot find a field on this page at all, use null - do not guess.
- Respond with ONLY a single JSON object, no markdown fences, no prose.
  Example: {{"total_assets": 4357382000, "cash_and_bank": 19233000}}
"""


def vision_extract_page(image_bytes, fields_needed, max_retries=3):
    """Send a page image (PNG bytes) to the free Gemini vision model and
    return {field: value_or_None} for the requested fields. Returns {}
    (not an exception) on any failure, so callers can treat this purely
    as a best-effort fallback layer."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {}

    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "model": GEMINI_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _build_prompt(fields_needed)},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}},
                ],
            }
        ],
        "max_tokens": 512,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=60)
        except requests.exceptions.RequestException:
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 429:
            time.sleep(5 * attempt)  # free-tier rate limit: back off and retry
            continue
        if resp.status_code != 200:
            return {}

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError):
            return {}

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}

        return {f: parsed.get(f) for f in fields_needed if f in parsed}

    return {}


def vision_locate_and_extract(fitz_doc, approx_page_index, fields_needed, search_radius=4, dpi=200):
    """The regex-based section classifier in extract_financials.py is
    brittle - it can point at the wrong page entirely (confirmed: it
    mistook an auditor's report page for the Balance Sheet on a real
    filing because the real title was missed and the rescue heuristic
    false-matched instead). Rather than trust a single page, try the
    classifier's guess first, then search outward (±1, ±2, ... pages)
    until a call returns at least one non-null value, capped at
    `search_radius` pages in each direction to bound API usage.

    Returns (result_dict, page_index_used) - page_index_used is None if
    nothing was found anywhere in the search window."""
    n_pages = fitz_doc.page_count
    offsets = [0]
    for d in range(1, search_radius + 1):
        offsets += [d, -d]

    for offset in offsets:
        page_index = approx_page_index + offset
        if page_index < 0 or page_index >= n_pages:
            continue
        pix = fitz_doc[page_index].get_pixmap(dpi=dpi)
        png_bytes = pix.tobytes("png")
        result = vision_extract_page(png_bytes, fields_needed)
        if any(v is not None for v in result.values()):
            return result, page_index

    return {}, None
