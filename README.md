# PSX Shariah Compliance Screener

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![OCR](https://img.shields.io/badge/OCR-Tesseract-success)
![Vision](https://img.shields.io/badge/fallback-Gemini%20Vision-orange)

Extracts the financial figures behind a PSX (Pakistan Stock Exchange)
company's KMI Shariah-compliance status — directly from its PDF report,
including scanned PDFs with no text layer at all.

## Why

The KMI-30/KMI-All compliance list tells you *whether* a company passed
the 6 Shariah screening rules, but not the numbers behind that verdict.
This tool pulls Total Assets, Revenue, Debt, Cash, and more straight out
of each company's own PDF report, so the result can be verified
independently instead of taken on faith.

## See it in action

```
> python extract_financials.py "annual_reports/ASC.pdf"

Sections found (page ranges): {'balance_sheet': (10, 10), 'profit_and_loss': (11, 11)}
Unit multiplier applied: 1000
Pages that needed OCR: [10, 11]

total_assets                 -> 4357382000.0  (p.10: "TOTAL ASSETS 4,357,382 5,336,449")
property_plant_equipment     -> 3590371000.0  (p.10: "Property, plant and equipment 6 3,590,371 3,775,033")
cash_and_bank                -> 19233000.0    (p.10: "Cash and bank balances 19,233 5,042")
total_revenue                -> 1610517000.0  (p.11: "Turnover 1,610,517 194,747 547,990 45,157")
other_income                 -> 137552000.0   (p.11: "Other income 137,552 23,667 - 23,667")
```

Every value is printed with the exact line it came from — nothing is
returned without a visible source.

## Quick start

```
pip install -r requirements.txt
winget install --id UB-Mannheim.TesseractOCR      # Windows OCR engine
python extract_financials.py "path/to/report.pdf"
```

## How it works

| Stage | Script | Does |
|---|---|---|
| 1. Collect | `collect.py` | Parses the KMI master PDF into a candidate company list |
| 2. Download | `downloader.py` | Downloads each company's report PDF, with retry + validation |
| 3. Extract | `extract_financials.py` | Pulls the Balance Sheet / P&L fields needed for verification |
| 3b. Fallback | `vision_fallback.py` | Optional free vision-LLM pass for anything still missing |

The extraction engine is built to survive real-world PDF inconsistency:

- **Section-aware search** — fields are only matched within the correct
  statement's pages, so a debt figure from the Cash Flow Statement can
  never be confused with the real Balance Sheet value.
- **Footnote-aware parsing** — distinguishes a line's real value from a
  bare footnote-reference number sitting next to the label.
- **Adaptive OCR** — pages are re-OCR'd whenever native text is missing,
  incomplete, or corrupted, not just when a page is visibly blank.
- **Row-reconstructed OCR** — rebuilds true row/column order from
  per-word bounding boxes, so wide multi-column tables don't get
  scrambled, and genuinely side-by-side statement layouts are detected
  and split apart automatically.
- **Free vision-LLM fallback** — unresolved fields can be handed to a
  free vision-capable model (Google Gemini), which reads the page image
  directly and searches nearby pages if the first guess was wrong.

## Setup

**Requirements:** Python 3.10+, [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) (Windows: `winget install --id UB-Mannheim.TesseractOCR`; macOS: `brew install tesseract`; Linux: `apt install tesseract-ocr`)

```
pip install -r requirements.txt
```

| Package | Used for |
|---|---|
| `pdfplumber` | PDF text/table extraction |
| `PyMuPDF` | Rendering PDF pages to images for OCR |
| `pytesseract` | Python wrapper around Tesseract OCR |
| `Pillow` | Image handling |
| `pandas` / `openpyxl` | Excel I/O |
| `requests` | Downloading report PDFs |

**Optional — vision fallback:** get a free key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey), then:

```
$env:GEMINI_API_KEY = "your-key-here"     # PowerShell
export GEMINI_API_KEY="your-key-here"     # macOS/Linux
```

## Usage

```
python collect.py                                    # Stage 1 (optional — company_list.csv is already included)
python downloader.py                                 # Stage 2 — fills ./annual_reports/
python extract_financials.py annual_reports/ASC.pdf  # Stage 3
python extract_financials.py annual_reports/ASC.pdf --vision   # Stage 3, with fallback
```
