"""
Section-aware financial statement extractor for PSX annual report PDFs.

Fixes the core failure mode of the old keyword-anywhere approach
(collect1.py's scan_pdf): that method searched the ENTIRE document for a
keyword and took the first number on the matching line, so a phrase like
"property, plant and equipment" inside the Cash Flow Statement's
"Additions to property, plant and equipment" line would get grabbed
instead of the actual Balance Sheet PP&E balance - wrong statement,
wrong number, no warning.

This version:
  1. Classifies every page into a financial-statement section (Balance
     Sheet / P&L / Cash Flow) by matching statement header text.
  2. Only searches for a field's keywords within the page range of the
     section that field actually belongs to.
  3. Detects the reporting unit scale ("Rupees in '000" / "in million")
     near each section and normalizes extracted numbers to full rupees.
  4. Falls back to OCR (PyMuPDF render -> pytesseract) per-page when a
     page has no extractable text layer, so scanned annual reports are
     handled instead of silently producing nothing.
"""

import os
import re
import pdfplumber
import fitz  # PyMuPDF
import pytesseract
from pytesseract import Output
from PIL import Image

from vision_fallback import vision_locate_and_extract

TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(TESSERACT_EXE):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE

# ---------------------------------------------------------------------
# Section headers -> which statement a page belongs to
# ---------------------------------------------------------------------
SECTION_PATTERNS = {
    "balance_sheet": [
        r"statement of financial position",
        r"balance sheet",
        r"statement of assets and liabilities",  # REIT filers (e.g. Dolmen City REIT)
    ],
    "profit_and_loss": [
        r"statement of profit or loss",
        r"profit and loss account",
        r"income statement",
    ],
    "cash_flow": [
        r"statement of cash flows?",
        r"cash flow statement",
    ],
}

# Headers for statements we DON'T extract fields from, but which must still
# close off the PRECEDING section's page range - otherwise a section with no
# header of its own (e.g. "Statement of Changes in Equity" sitting between
# the Balance Sheet and the Cash Flow Statement) gets silently absorbed into
# the previous section's inferred range, re-introducing cross-statement
# contamination even with section-scoping in place.
BOUNDARY_PATTERNS = [
    r"statement of comprehensive income",
    r"other comprehensive income",
    r"statement of changes in equity",
    r"statement of changes in unit holders",
    r"notes to the( condensed)?( interim)? financial statements",
    r"independent auditor'?s? (review )?report",
    r"directors'? report",
]
# A page that has moved on to numbered prose notes (e.g. "1. LEGAL STATUS
# AND NATURE OF BUSINESS", "2. BASIS OF PREPARATION") is never still inside
# a primary financial statement, regardless of header detection.
NOTES_HEADING_RE = re.compile(r"^\d{1,2}\.\s+[A-Z][A-Z \-,&']{8,}$")

# Which section each field must be read from
FIELD_SECTION = {
    "total_assets": "balance_sheet",
    "total_liabilities": "balance_sheet",
    "long_term_debt": "balance_sheet",
    "short_term_debt": "balance_sheet",
    "investments": "balance_sheet",
    "property_plant_equipment": "balance_sheet",
    "stock_in_trade": "balance_sheet",
    "cash_and_bank": "balance_sheet",
    "total_revenue": "profit_and_loss",
    "other_income": "profit_and_loss",
}

FIELD_KEYWORDS = {
    "total_assets": ["total assets"],
    "total_liabilities": ["total liabilities"],
    "long_term_debt": ["long term financing", "long-term financing", "long term borrowings",
                        "long-term borrowings", "term finance certificates", "sukuk"],
    "short_term_debt": ["short term borrowings", "short-term borrowings", "running finance",
                         "running finances", "current portion of long term financing"],
    "investments": ["short term investments", "long term investments", "investment property"],
    "other_income": ["other income", "profit on bank deposits", "mark-up income",
                      "markup income", "interest income"],
    "total_revenue": ["revenue from contracts with customers", "net sales", "sales - net",
                       "turnover", "total revenue", "net revenue"],
    "property_plant_equipment": ["property, plant and equipment", "property plant and equipment",
                                  "operating fixed assets", "fixed assets - net"],
    "stock_in_trade": ["stock-in-trade", "stock in trade", "inventories"],
    "cash_and_bank": ["cash and bank balances", "cash and cash equivalents"],
}

NUMBER_RE = re.compile(r"\(?-?[\d,]+\.?\d*\)?")

UNIT_PATTERNS = [
    # tolerate OCR misreading the straight apostrophe in "'000'" as a
    # curly quote or other stray punctuation (e.g. "Rupees in '000'")
    (re.compile(r"rupees?\s+in\W{0,3}000\b|rupees?\s+in\s+thousands?", re.I), 1_000),
    (re.compile(r"rupees?\s+in\s+million|amounts?\s+in\s+pkr\s+million", re.I), 1_000_000),
]


# ---------------------------------------------------------------------
# Text acquisition: native text layer, else OCR via PyMuPDF + tesseract
# ---------------------------------------------------------------------

def _is_font_corrupted(text, sample_len=2000):
    """Some PDFs embed a subset font with a broken/missing ToUnicode CMap,
    so pdfplumber "successfully" extracts text but it's literal glyph-ID
    placeholders like "(cid:39)(cid:82)..." instead of real characters -
    this looks like a populated text layer but is useless for matching."""
    sample = text[:sample_len]
    return sample.count("(cid:") >= 5


def page_text_with_ocr_fallback(plumber_page, fitz_page, dpi=300):
    text = plumber_page.extract_text() or ""
    if text.strip() and not _is_font_corrupted(text):
        return text, False
    return ocr_page(fitz_page, dpi=dpi), True


def ocr_page(fitz_page, dpi=300):
    """Row-reconstructed OCR: pytesseract.image_to_string() runs page
    segmentation (--psm 3 by default) that reads wide-gapped multi-column
    financial tables column-by-column instead of row-by-row, scrambling
    which number belongs to which label (confirmed on real PSX filings -
    e.g. a label ends up with no adjacent number while an unrelated row's
    number lands next to it instead). Using image_to_data() with --psm 6
    (treat page as one uniform block, don't try to split columns) gives
    per-word bounding boxes; clustering those by y-center and sorting each
    cluster left-to-right by x rebuilds the actual visual row order."""
    pix = fitz_page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    # NOTE: attempted a tessedit_char_whitelist here to stop Tesseract from
    # occasionally misreading a bold leading digit as a stray symbol (e.g.
    # "5,461,..." -> "§,461,...", seen on a real filing) - reverted because
    # pytesseract shlex-splits the config string, and getting a value
    # containing a literal space/apostrophe through that quoting correctly
    # on Windows proved unreliable and broke word-spacing entirely when it
    # went wrong. Documented as a known rare-symbol-misread limitation
    # instead of risking a more fragile fix.
    data = pytesseract.image_to_data(img, config="--psm 6", output_type=Output.DICT)

    words = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        top, height = data["top"][i], data["height"][i]
        words.append({
            "text": text,
            "x": data["left"][i],
            "y_center": top + height / 2,
            "height": height,
        })

    if not words:
        return ""

    page_width = pix.width
    column_groups = _split_into_columns(words, page_width)

    all_lines = []
    for group in column_groups:
        group.sort(key=lambda w: w["y_center"])
        median_height = sorted(w["height"] for w in group)[len(group) // 2] or 10

        rows = []
        current_row = [group[0]]
        for w in group[1:]:
            if abs(w["y_center"] - current_row[-1]["y_center"]) <= 0.6 * median_height:
                current_row.append(w)
            else:
                rows.append(current_row)
                current_row = [w]
        rows.append(current_row)

        for row in rows:
            row.sort(key=lambda w: w["x"])
            all_lines.append(" ".join(w["text"] for w in row))

    return "\n".join(all_lines)


def _split_into_columns(words, page_width, n_bins=40):
    """Detect a vertical gutter (a side-by-side two-statement page layout,
    e.g. Balance Sheet and P&L printed next to each other in one page) by
    finding a near-empty vertical band in the middle third of the page.
    Without this, row-clustering-by-y merges two unrelated statements'
    lines together just because they sit at the same height on the page.
    Returns a list of word-groups (1 group = single column, 2 = split)."""
    bin_width = page_width / n_bins
    counts = [0] * n_bins
    for w in words:
        b = min(int(w["x"] / bin_width), n_bins - 1)
        counts[b] += 1

    avg = sum(counts) / n_bins or 1
    middle_lo, middle_hi = int(n_bins * 0.33), int(n_bins * 0.67)
    middle_bins = range(middle_lo, middle_hi)
    gutter_bin = min(middle_bins, key=lambda b: counts[b], default=None)

    if gutter_bin is None or counts[gutter_bin] > 0.15 * avg:
        return [words]  # no clear gutter - treat as single column

    gutter_x = (gutter_bin + 0.5) * bin_width
    left = [w for w in words if w["x"] < gutter_x]
    right = [w for w in words if w["x"] >= gutter_x]
    if not left or not right:
        return [words]

    # Every single-statement page has a visual gap between its label column
    # and its numbers column - that gap alone does NOT mean there are two
    # independent statements side by side. A single statement's right side
    # still has a FEW alphabetic words (column headers like "Un-Audited",
    # "Note", "Rupees in '000'") but they're all clustered in the first
    # row or two. A genuine second statement has its own label text spread
    # across many distinct rows down the whole page. So: count how many
    # DISTINCT row-heights on the right contain alphabetic words, not just
    # what fraction of words are alphabetic.
    def distinct_label_rows(group):
        alpha_words = [w for w in group if any(c.isalpha() for c in w["text"])]
        if not alpha_words:
            return 0
        heights = sorted(w["height"] for w in alpha_words)
        median_h = heights[len(heights) // 2] or 10
        ys = sorted(w["y_center"] for w in alpha_words)
        rows = 1
        for a, b in zip(ys, ys[1:]):
            if b - a > 0.6 * median_h:
                rows += 1
        return rows

    if distinct_label_rows(right) < 15:
        return [words]  # just a header strip (dates/Note/units) - same statement, don't split

    return [left, right]


# Sanity vocabulary: if a page's native text layer is missing ALL of these
# words for its section, the row LABELS are likely non-extractable glyphs
# (seen in some PSX-generated PDFs: numbers are real text, labels are
# outlined/vector-only) even though the page isn't fully textless. Force
# OCR in that case so labels can actually be matched against keywords.
SECTION_SANITY_WORDS = {
    "balance_sheet": ["assets", "liabilities", "equity"],
    "profit_and_loss": ["revenue", "sales", "profit", "turnover"],
    "cash_flow": ["cash flow", "operating activities", "financing activities"],
}


HEADER_LINE_MAX_LEN = 60  # real statement titles run 31-41 chars; TOC entries
                           # and boilerplate mentions run 69+ chars - wide gap,
                           # so 60 gives margin for stray OCR artifacts (e.g. a
                           # logo glyph merged onto the header row) without
                           # risking false positives from the long lines


def classify_sections(pages_text):
    """Return {section_name: (start_page, end_page)} using 1-indexed pages.

    Only counts a header match if it's a short, near-standalone line - this
    is what separates an actual statement title ("STATEMENT OF FINANCIAL
    POSITION", ~31 chars) from a table-of-contents entry ("... Statement of
    Financial Position 10", ~69 chars) or a boilerplate audit-report mention
    ("...statement of financial position, unconsolidated...", ~98 chars).
    """
    hits = []  # (page_num, section), first real occurrence only
    boundary_pages = set()  # pages where some OTHER statement/section starts,
                             # closing off whatever primary statement precedes them
    seen_sections = set()
    for page_num, text in enumerate(pages_text, start=1):
        lines = text.split("\n")
        for idx, line in enumerate(lines):
            low = line.lower().strip()
            if not low or low.startswith(("•", "-", "*")):
                continue

            if NOTES_HEADING_RE.match(line.strip()):
                boundary_pages.add(page_num)
                continue

            # boundary headers get a looser length allowance than primary
            # statement titles - false positives here only end a section's
            # range a page or two early, far cheaper than missing the
            # boundary and letting contamination back in
            if len(low) <= 90 and any(re.search(p, low) for p in BOUNDARY_PATTERNS):
                boundary_pages.add(page_num)
                continue

            # Length alone can't fully separate real titles from false
            # positives - some filers prepend words like "REISSUED" and
            # push a genuine title past the usual length (confirmed on a
            # real scanned filing: "REISSUED CONDENSED INTERIM STATEMENT OF
            # FINANCIAL POSITION (UN-AUDITED)" is 73 chars, same ballpark as
            # a rejected boilerplate false-positive). A real statement title
            # is always immediately followed by an "AS AT <date>" or "FOR
            # THE ... PERIOD ENDED" line - TOC entries and boilerplate
            # mentions never are - so that's a reliable rescue signal for
            # titles that run long.
            if len(low) > HEADER_LINE_MAX_LEN:
                next_low = lines[idx + 1].lower().strip() if idx + 1 < len(lines) else ""
                # search, not match-anchored: OCR often prepends stray noise
                # characters (confirmed: "’ * AS AT DECEMBER 31, 2025")
                if not re.search(r"\bas (at|on)\b|\bfor the\b.{0,40}\bended\b", next_low):
                    continue

            for section, patterns in SECTION_PATTERNS.items():
                if section in seen_sections:
                    continue
                if any(re.search(p, low) for p in patterns):
                    hits.append((page_num, section))
                    seen_sections.add(section)
                    break

    hits.sort()
    all_stop_pages = sorted(boundary_pages | {p for p, _ in hits})
    ranges = {}
    for i, (page_num, section) in enumerate(hits):
        next_hit = hits[i + 1][0] if i + 1 < len(hits) else None
        # the nearest later stop point - either the next statement header we
        # DO care about, or a boundary header for one we don't - whichever
        # comes first closes this section's range
        later_stops = [p for p in all_stop_pages if p > page_num]
        next_stop = min([p for p in [next_hit] + later_stops if p is not None], default=None)
        end = (next_stop - 1) if next_stop is not None else page_num + 2
        ranges[section] = (page_num, max(end, page_num))
    return ranges


def detect_unit_multiplier(text):
    for pattern, mult in UNIT_PATTERNS:
        if pattern.search(text):
            return mult
    return 1


def extract_numbers(line):
    """Returns financial values on the line, filtering out bare "Note"
    column reference numbers (e.g. the "5" in "Property, plant and
    equipment 5 3,062,818,499 3,093,921,761") which otherwise get
    mistaken for the actual balance since they're the first number on
    the line. PSX statements commonly interleave a 1-2 digit note
    reference between the label and the real (comma-formatted) values,
    so: if the line contains at least one comma-formatted number, only
    bare small integers (no comma, < 1000) are treated as note refs and
    dropped; if NONE of the numbers on the line have commas, nothing is
    dropped (handles genuinely small non-note figures)."""
    candidates = []  # (value, had_comma)
    for m in NUMBER_RE.findall(line):
        neg = m.startswith("(") and m.endswith(")")
        had_comma = "," in m
        val = m.strip("()").replace(",", "")
        try:
            num = float(val)
            if num != 0 or len(val) > 1:
                candidates.append((-num if neg else num, had_comma))
        except ValueError:
            continue

    any_comma = any(c for _, c in candidates)
    if not any_comma:
        return [v for v, _ in candidates]

    return [v for v, had_comma in candidates if had_comma or abs(v) >= 1000]


# ---------------------------------------------------------------------
# Main per-PDF scan
# ---------------------------------------------------------------------

def scan_pdf(pdf_path, use_vision_fallback=False):
    """Returns (results, meta). results[field] = list of candidate matches.
    meta has ocr_pages_used, sections_found, unit_multiplier."""
    with pdfplumber.open(pdf_path) as pdf, fitz.open(pdf_path) as doc:
        pages_text = []
        ocr_pages = []
        for i, page in enumerate(pdf.pages):
            text, used_ocr = page_text_with_ocr_fallback(page, doc[i])
            pages_text.append(text)
            if used_ocr:
                ocr_pages.append(i + 1)

        sections = classify_sections(pages_text)

        # Force OCR on statement pages whose native text is missing the
        # section's expected vocabulary (labels rendered as non-text glyphs).
        for section, (start, end) in sections.items():
            sanity_words = SECTION_SANITY_WORDS.get(section, [])
            if not sanity_words:
                continue
            for page_num in range(start, min(end, len(pages_text)) + 1):
                current = pages_text[page_num - 1]
                low = current.lower()
                # a coincidental keyword hit in the page TITLE (e.g. "PROFIT
                # OR LOSS" header) doesn't prove the statement BODY extracted
                # - a financial statement table has many comma-formatted
                # numbers (e.g. "1,610,517"); too few means the data table
                # itself failed to extract even if boilerplate text did.
                few_numbers = len(re.findall(r"\d{1,3}(?:,\d{3})+", current)) < 3
                if few_numbers or not any(w in low for w in sanity_words):
                    pages_text[page_num - 1] = ocr_page(doc[page_num - 1])
                    if page_num not in ocr_pages:
                        ocr_pages.append(page_num)

        unit_multiplier = 1
        for section_range in sections.values():
            start, end = section_range
            for p in range(start - 1, min(end, len(pages_text))):
                m = detect_unit_multiplier(pages_text[p])
                if m != 1:
                    unit_multiplier = m
                    break

        results = {field: [] for field in FIELD_KEYWORDS}
        for field, keywords in FIELD_KEYWORDS.items():
            section = FIELD_SECTION[field]
            if section not in sections:
                continue
            start, end = sections[section]
            for page_num in range(start, min(end, len(pages_text)) + 1):
                text = pages_text[page_num - 1]
                for line in text.split("\n"):
                    low = line.lower().strip()
                    if not low:
                        continue
                    for kw in keywords:
                        if kw in low:
                            nums = extract_numbers(line)
                            if nums:
                                results[field].append({
                                    "page": page_num,
                                    "line": line.strip(),
                                    "values": [n * unit_multiplier for n in nums],
                                })
                            break

        vision_fields_used = []
        if use_vision_fallback:
            missing_by_section = {}
            for field in FIELD_KEYWORDS:
                if results[field]:
                    continue
                section = FIELD_SECTION[field]
                if section not in sections:
                    continue
                missing_by_section.setdefault(section, []).append(field)

            for section, missing_fields in missing_by_section.items():
                start, _ = sections[section]
                # the regex-based section classifier can point at the wrong
                # page entirely (confirmed: mistook an auditor's report page
                # for the Balance Sheet on a real filing) - search outward
                # from its guess instead of trusting it blindly
                extracted, found_page_index = vision_locate_and_extract(
                    doc, start - 1, missing_fields
                )
                if found_page_index is None:
                    continue
                for field, value in extracted.items():
                    if value is None:
                        continue
                    results[field].append({
                        "page": found_page_index + 1,
                        "line": "(vision-LLM fallback: no matching line found by keyword search)",
                        "values": [float(value)],
                    })
                    vision_fields_used.append(field)

        meta = {
            "ocr_pages_used": ocr_pages,
            "sections_found": {k: v for k, v in sections.items()},
            "unit_multiplier": unit_multiplier,
            "has_any_text": any(t.strip() for t in pages_text),
            "vision_fields_used": vision_fields_used,
        }
        return results, meta


def best_candidate(field_results):
    if not field_results:
        return None, ""
    first = field_results[0]
    value = first["values"][0] if first["values"] else None
    evidence = f"p.{first['page']}: \"{first['line'][:90]}\""
    return value, evidence


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_vision = "--vision" in sys.argv
    path = args[0] if args else None
    if not path:
        print("Usage: python extract_financials.py <path-to-annual-report.pdf> [--vision]")
        print("  --vision   fill in still-missing fields using the free Gemini")
        print("             vision-LLM fallback (requires GEMINI_API_KEY env var)")
        raise SystemExit(1)

    results, meta = scan_pdf(path, use_vision_fallback=use_vision)
    print("Sections found (page ranges):", meta["sections_found"])
    print("Unit multiplier applied:", meta["unit_multiplier"])
    print("Pages that needed OCR:", meta["ocr_pages_used"])
    if use_vision:
        print("Fields filled by vision-LLM fallback:", meta["vision_fields_used"])
    print()
    for field in FIELD_KEYWORDS:
        val, ev = best_candidate(results[field])
        print(f"{field:28s} -> {val}  ({ev})")
