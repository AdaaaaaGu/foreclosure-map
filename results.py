"""Attach a foreclosure *results* list (sold prices) to a book.

Once any results are attached, every row without a sold price is marked "Unsold".

The results PDF layout isn't known yet, so parse_results_ocr() is deliberately generic: it finds
each OCR'd table line that contains a street address and picks up the dollar amount on that same
line (preferring a column whose header mentions SOLD / SALE / BID / PRICE). Once a real results
book is available, give it a dedicated column parser like parser.py.
CSV / JSON uploads work today: columns id and/or address, plus soldPrice (or price/sold/amount).
"""
import csv
import io
import re
import time

import parser as P

SUFFIX = {"DRIVE": "DR", "STREET": "ST", "LANE": "LN", "ROAD": "RD", "AVENUE": "AVE", "BOULEVARD": "BLVD",
          "COURT": "CT", "CIRCLE": "CIR", "COVE": "CV", "TRAIL": "TRL", "PARKWAY": "PKWY", "PLACE": "PL",
          "TERRACE": "TER", "HIGHWAY": "HWY", "LOOP": "LOOP", "PASS": "PASS", "TRACE": "TRCE", "WAY": "WAY"}
MONEY_TOKEN = re.compile(r"\$\s?-?\d{1,3}(?:,\d{3})+(?:\.\d\d)?|\$\s?\d+(?:\.\d\d)?")
STATUS_WORDS = [("CANCEL", "Cancelled"), ("POSTPON", "Postponed"), ("NO SALE", "Unsold"),
                ("UNSOLD", "Unsold"), ("STRUCK", "Unsold"), ("SOLD", "Sold")]


def norm_addr(s):
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    words = [SUFFIX.get(w, w) for w in s.split()]
    words = [w for w in words if w not in ("UNIT", "APT", "STE", "BLDG")]
    return " ".join(words)


def addr_key(s):
    """House number + first street word: robust to suffix/unit/OCR noise at the end."""
    w = norm_addr(s).split()
    if len(w) >= 2 and w[0].isdigit():
        return f"{w[0]} {w[1]}"
    return norm_addr(s)


def parse_results_csv(text):
    out = []
    for rec in csv.DictReader(io.StringIO(text)):
        low = {k.strip().lower(): (v or "").strip() for k, v in rec.items() if k}
        price = next((low[k] for k in ("soldprice", "sold price", "sold", "price", "amount", "sale price")
                      if low.get(k)), None)
        out.append({"id": int(low["id"]) if low.get("id", "").isdigit() else None,
                    "address": low.get("address") or low.get("street"),
                    "soldPrice": P.money(price) if price else None,
                    "status": low.get("status") or None,
                    "buyer": low.get("buyer") or None})
    return out


def parse_results_ocr(ocr_path):
    entries = []
    for page in P.load_pages(ocr_path):
        lines = page["lines"]
        # column hint from a header like "SOLD PRICE" / "SALE AMOUNT" / "WINNING BID"
        price_x = None
        for l in lines:
            if re.search(r"SOLD|SALE\s*(PRICE|AMOUNT)|BID|PRICE", l["t"].upper()) and l["y"] < 0.2:
                price_x = (l["x"] - 0.03, l["x"] + l["w"] + 0.03)
        # group lines into visual rows
        rows = []
        for l in sorted(lines, key=lambda l: l["y"]):
            if rows and abs(rows[-1][0]["y"] - l["y"]) < 0.006:
                rows[-1].append(l)
            else:
                rows.append([l])
        for i, row in enumerate(rows):
            street = next((l for l in row if re.match(r"^\d{1,6}\s+[A-Z0-9]", l["t"].upper())
                           and not MONEY_TOKEN.search(l["t"])), None)
            if not street:
                continue
            band = row + (rows[i + 1] if i + 1 < len(rows) else [])
            monies = [(l, m.group()) for l in band for m in MONEY_TOKEN.finditer(l["t"])]
            price = None
            if price_x:
                inside = [v for l, v in monies if price_x[0] <= l["x"] + l["w"] / 2 <= price_x[1]]
                price = inside[0] if inside else None
            if price is None and monies:
                price = monies[0][1]
            text = " ".join(l["t"] for l in band).upper()
            status = next((s for k, s in STATUS_WORDS if k in text), None)
            entries.append({"address": street["t"], "soldPrice": P.money(price) if price else None,
                            "status": status, "raw": " | ".join(l["t"] for l in row)})
    return entries


def norm_status(s):
    u = (s or "").upper()
    return next((v for k, v in STATUS_WORDS if k in u), None)


def refresh_status(book):
    if not book["meta"].get("resultsAttached"):
        return
    for r in book["rows"]:
        if r.get("soldPrice"):
            r["status"] = r.get("status") if r.get("status") not in (None, "Unsold") else "Sold"
        elif r.get("status") in (None, "Sold"):
            r["status"] = "Unsold"


def merge(book, entries, source="results"):
    by_id = {r["id"]: r for r in book["rows"]}
    by_addr = {}
    for r in book["rows"]:
        by_addr.setdefault(addr_key(r.get("street")), []).append(r)
    matched = 0
    unmatched = []
    for e in entries:
        target = None
        if e.get("address"):
            cands = by_addr.get(addr_key(e["address"]), [])
            target = cands[0] if len(cands) == 1 else None
        if target is None and e.get("id") in by_id:
            target = by_id[e["id"]]
        if target is None:
            unmatched.append(e.get("address") or e.get("id"))
            continue
        matched += 1
        if e.get("soldPrice"):
            target["soldPrice"] = e["soldPrice"]
        status = norm_status(e.get("status"))
        if status:
            target["status"] = status
        if e.get("buyer"):
            target["buyer"] = e["buyer"]
        target["resultSource"] = source
    book["meta"]["resultsAttached"] = True
    book["meta"]["resultsSource"] = source
    book["meta"]["resultsAt"] = time.time()
    refresh_status(book)
    sold = sum(1 for r in book["rows"] if r.get("status") == "Sold")
    return {"entries": len(entries), "matched": matched, "unmatched": unmatched[:50], "sold": sold,
            "unsold": sum(1 for r in book["rows"] if r.get("status") == "Unsold")}
