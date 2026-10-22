# Foreclosure Map

A Zillow-style map for Roddy's FLS foreclosure books (scanned PDFs).

```
./run.sh          # compiles the OCR helper once, starts http://localhost:8765 and opens it
```

Upload a book PDF, then wait about a minute. The pages are read with macOS Vision OCR, parsed into rows, and geocoded (US Census, with ArcGIS as a fallback). Each posting shows up as a pin and a card.

- **Hover** a card or pin to see a quick summary. **Click** one for every field in the row, plus links to Zillow, Google Maps, and Street View.
- Filter by city, home type, loan type, equity %, and value. You can also search by address, owner, lender, or subdivision.
- **Add results** attaches that month's results list, which gives the sold price. Postings that don't appear in the results are marked Unsold. You can upload a CSV (`address,sold price,status` or `id,…`) or a PDF. You can also set a result by hand in the detail panel.

Files:
- `ocr/pdf_ocr.swift`: renders the pages and runs Vision OCR, once on the full page and once per column tile.
- `parser.py`: turns the OCR output into table rows. Run `python3 parser.py <ocr.jsonl> --dump` to debug.
- `geocode.py`, `results.py`, `server.py`, `web/`
- `data/`: saved books, OCR cache, and geocode cache. Delete `data/cache` to force a re-read.
