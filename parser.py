"""Parse OCR output of a Roddy's FLS "Foreclosure List" book into structured rows.

Input: the JSON-lines stream produced by ocr/pdf_ocr (one {"type":"page"} object per page,
each with text lines in normalized page coordinates, y measured from the top).

Usage: python3 parser.py <ocr.jsonl> [--dump]
"""
import difflib
import json
import re
import sys

# Column boundaries (normalized x), tested against the line's horizontal center.
# ID | ADDRESS | CLASS | LEGAL | MORTGAGOR | DATE | ORG AMT | MOPMT | EQUITY
COLS = ["id", "address", "class", "legal", "parties", "loan", "amounts", "payment", "equity"]
CENTER_BOUNDS = [0.092, 0.247, 0.312, 0.492, 0.640, 0.745, 0.840, 0.897]
# Physical column edges, used to split OCR lines that ran across two columns.
EDGE_BOUNDS = [0.095, 0.248, 0.316, 0.497, 0.643, 0.748, 0.843, 0.900]
# Column strips passed to ocr/pdf_ocr (slightly overlapping the edges).
OCR_STRIPS = "0.048-0.095,0.095-0.249,0.248-0.325,0.316-0.499,0.496-0.644,0.642-0.749,0.747-0.844,0.842-0.901,0.899-0.968"

MONEY_RE = re.compile(r"\$?\s*(-?)\$?\s*([\d,]+(?:\.\d+)?)")
# "78745 $777,160" -- tolerant of OCR noise such as "7U55 5900,450"
ZIP_LINE_RE = re.compile(r"^(\d{5}|\S{4,5}\s+[\$S57]?\s*[\d,.]{5,}\S*|\d{4,5}\s*\$.*)$")
LENDER_WORDS = {
    "LLC", "INC", "NA", "N.A.", "BANK", "CORP", "CORPORATION", "TRUST", "FSB", "MORTGAGE", "MTG",
    "LOAN", "LOANS", "SERVICING", "SERVICES", "LENDING", "SAVINGS", "HOLDINGS", "FINANCIAL", "CREDIT",
    "UNION", "ASSOCIATION", "ASSN", "FUND", "CAPITAL", "COMPANY", "HOA", "OWNERS", "LP", "LTD",
    "FEDERAL", "NEWREZ", "PENNYMAC", "MIDFIRST", "COMERICA", "DISTRICT", "MUNICIPAL", "COUNCIL",
    "PARTNERS", "INVESTMENTS", "ACCEPTANCE", "COMMUNITY", "CONDOMINIUM", "PROPERTY", "CU", "FCU",
    "FINANCE", "PLC", "SERVICER", "FUNDING", "INSURANCE",
}
CORP_SUFFIX = {"LLC", "INC", "LTD", "LP", "CO", "PLC", "NA", "CORP"}
# Words that continue a lender name started on the previous line ("PLANET HOME / LENDING LLC").
CONTINUATION_START = CORP_SUFFIX | {"CREDIT", "UNION", "LENDING", "SERVICING", "SERVICES", "CORPORATION",
                                    "MORTGAGE", "TRUST", "FINANCE", "ASSN", "ASSOCIATION", "COMMUNITY",
                                    "CONDOMINIUM", "BANK", "OWNERS", "FUND", "LOAN", "SAVINGS", "FINANCIAL",
                                    "INSURANCE"}
LOAN_TYPES = ["Fannie Mae / Freddie Mac", "Reverse Mortgage", "Home Equity", "Construction",
              "Conventional", "Commercial", "FHA", "VA", "ARM", "HOA", "USDA", "Tax", "Private"]


def money(s):
    """'$-66,927' -> -66927 ; returns None if no digits."""
    if not s:
        return None
    s = s.replace("O", "0").replace("o", "0").replace(" ", "")
    s = re.sub(r"(\d)\.(\d{3})(?!\d)", r"\1,\2", s)  # "7557.324" -> "7557,324"
    # OCR often reads "$" as 5/7/S: "5557,334" -> "$557,334" (4 digits before a comma is impossible)
    m = re.match(r"^(-?)[S57](-?\d{1,3}(?:,\d{3})+)$", s)
    if m and "$" not in s:
        s = "$" + m.group(1) + m.group(2)
    m = MONEY_RE.search(s)
    if not m:
        return None
    digits = m.group(2).replace(",", "")
    try:
        v = float(digits)
    except ValueError:
        return None
    v = int(v) if v.is_integer() else v
    return -v if m.group(1) == "-" or s.startswith("-") else v


# Vision sometimes returns Cyrillic/Greek look-alikes ("Р" for "P") on this all-caps table.
LOOKALIKES = str.maketrans("АВСЕНІКМОРТХУаеорсухЅՕ‹›", "ABCEHIKMOPTXYaeopcyxSO<>")


def load_pages(path):
    pages = []
    with open(path) as f:
        for line in f:
            o = json.loads(line)
            if o.get("type") == "page":
                for l in o["lines"]:
                    l["t"] = l["t"].translate(LOOKALIKES)
                pages.append(o)
    return pages


def is_list_page(lines):
    txt = " ".join(l["t"] for l in lines).upper()
    return "FORECLOSURE LIST" in txt and "ORG AMT" in txt and "MORTGAGOR" in txt


def col_index(center):
    for i, b in enumerate(CENTER_BOUNDS):
        if center < b:
            return i
    return len(CENTER_BOUNDS)


def split_spanning(line):
    """Split an OCR line that crosses a column edge into per-column pieces."""
    x0, x1, t = line["x"], line["x"] + line["w"], line["t"]
    pieces = []
    cur_x, cur_t = x0, t
    for edge in EDGE_BOUNDS:
        if cur_x + 0.012 < edge < x1 - 0.012 and len(cur_t) > 3:
            frac = (edge - cur_x) / (x1 - cur_x)
            k = int(round(frac * len(cur_t)))
            # snap to nearest space
            best = None
            for d in range(0, 8):
                for j in (k - d, k + d):
                    if 0 < j < len(cur_t) and cur_t[j] == " ":
                        best = j
                        break
                if best is not None:
                    break
            if best is None:
                continue
            left, right = cur_t[:best].strip(), cur_t[best:].strip()
            if left:
                pieces.append(dict(line, x=cur_x, w=edge - cur_x, t=left))
            cur_x, cur_t = edge, right
    pieces.append(dict(line, x=cur_x, w=x1 - cur_x, t=cur_t))
    return [p for p in pieces if p["t"]]


MONEY_OK = r"\$-?\d{1,3}(,\d{3})*"
VALID_LINE = {
    "address": [r"\d{5} " + MONEY_OK],
    "class": [r"(1[89]|20)\d\d/\s*\d*", r"Single|Family|Residential|Condominiu|m|Townhouse|Land|Commercial|Multifamily"],
    "loan": [r"\d{1,2}/(19|20)\d\d", r"\d{1,2}\.\d{1,3}", r"FHA|VA|ARM|HOA|Conventional|Commercial|Fannie Mae /|Freddie Mac|Home Equity|Construction"],
    "amounts": [MONEY_OK, r"\d{10}", r"\d{4}/\d+"],
    "payment": [r"\d{2,6}", r"\d{1,2}-\d", r"\d{1,2}/(19|20)\d\d"],
    "equity": [MONEY_OK, r"-?\d{1,3}%"],
}


def _fix_starts_with_ids(starts, addr, id_cells):
    """ID numbers sit vertically centered in their row, so exactly one row start must fall
    between consecutive ID centers. Use that to split merged rows / merge split rows."""
    ids = sorted((c["y"] + c["h"] / 2, int(c["t"].strip())) for c in id_cells
                 if re.fullmatch(r"\d{1,4}", c["t"].strip()))
    # drop IDs that break the ascending sequence (OCR misreads)
    clean = []
    for y, v in ids:
        if not clean or 0 < v - clean[-1][1] <= 3:
            clean.append((y, v))
    ids = clean
    if len(ids) < 2:
        return starts
    starts = sorted(starts)
    out = [s for s in starts if s < ids[0][0]] or starts[:1]
    for (a, va), (b, vb) in zip(ids, ids[1:]):
        expected = vb - va  # usually 1; more when an ID was not read
        inside = [s for s in starts if a < s < b]
        if len(inside) >= expected:
            out.extend(inside[-expected:])  # extra starts were continuation lines
        elif inside:
            out.extend(inside)
        else:
            cands = [c for c in addr if a < c["y"] < b and re.match(r"^\d+\s+\S", c["t"].strip())]
            if cands:
                # the street line with the biggest gap above it
                def gap(c):
                    above = [d["y"] for d in addr if d["y"] < c["y"]]
                    return c["y"] - max(above) if above else 1
                out.append(max(cands, key=gap)["y"])
    return sorted(set(out))


def parse_page(page):
    full = [l for l in page["lines"] if not l.get("s")]
    stripped = [l for l in page["lines"] if l.get("s")]
    lines = full
    header_bottom = 0.066
    footer_top = 0.93
    month = None
    for l in lines:
        m = re.search(r"(\d{1,2}/\d{4})\s*Page", l["t"])
        if m:
            month = m.group(1)
        if l["t"].strip().upper().startswith("MARGIN"):
            header_bottom = max(header_bottom, l["y"] + 0.006)
        if l["t"].upper().startswith("COPYRIGHT"):
            footer_top = min(footer_top, l["y"] - 0.002)

    comments = [l for l in full if header_bottom < l["y"] < footer_top
                and l["x"] < 0.09 and l["t"].strip().upper().startswith("COMMENT")]
    def in_body(l):
        return (header_bottom < l["y"] < footer_top and l not in comments
                and not any(abs((l["y"] + l["h"] / 2) - (c["y"] + c["h"] / 2)) < c["h"] * 0.5 for c in comments))

    cells = []
    for l in full:
        if in_body(l):
            for p in split_spanning(l):
                cells.append(dict(p, col=col_index(p["x"] + p["w"] / 2)))
    # Per-column OCR pass: its strip index is the column.
    strip_cells = [dict(l, col=l["s"] - 1) for l in stripped if in_body(l) and 0 <= l["s"] - 1 < len(COLS)]

    # Row starts: in the address column, a row ends with the "ZIP $VALUE" line; the next address line
    # starts a new row.
    addr = sorted([c for c in cells if c["col"] == 1], key=lambda c: c["y"])
    starts = []
    expect_start = True
    for c in addr:
        if expect_start:
            starts.append(c["y"])
            expect_start = False
        if ZIP_LINE_RE.match(c["t"].strip()):
            expect_start = True
    starts = _fix_starts_with_ids(starts, addr, [c for c in cells if c["col"] == 0])
    # Other columns can begin slightly above the address line.
    bands = []
    for i, y in enumerate(starts):
        top = y - 0.006
        bottom = starts[i + 1] - 0.006 if i + 1 < len(starts) else footer_top
        bands.append((top, bottom))

    rows = []
    def alnum(ls):
        return sum(ch.isalnum() for l in ls for ch in l)

    def score(name, ls):
        """Lines that look like valid values for this column count far more than raw characters."""
        pats = VALID_LINE.get(name)
        valid = sum(1 for l in ls if pats and any(re.fullmatch(p, l.strip()) for p in pats))
        return valid * 100 + alnum(ls)

    for top, bottom in bands:
        cols = {}
        for ci, name in enumerate(COLS):
            a = [c for c in sorted(cells, key=lambda c: (c["y"], c["x"])) if c["col"] == ci and top <= c["y"] < bottom]
            b = [c for c in sorted(strip_cells, key=lambda c: (c["y"], c["x"])) if c["col"] == ci and top <= c["y"] < bottom]
            ta, tb = [c["t"].strip() for c in a], [c["t"].strip() for c in b]
            # Whichever OCR pass recovered more text for this cell wins (dropped lines / clipped letters).
            best, other = (b, a) if score(name, tb) > score(name, ta) else (a, b)
            best = list(best)
            pats = VALID_LINE.get(name)
            if pats:
                # For structured columns, also borrow valid lines the winning pass missed entirely.
                for c in other:
                    t = c["t"].strip()
                    dup = any(abs(c["y"] - d["y"]) < 0.005 or
                              (abs(c["y"] - d["y"]) < 0.02 and d["t"].strip() == t) for d in best)
                    if any(re.fullmatch(p, t) for p in pats) and not dup:
                        best.append(c)
                best.sort(key=lambda c: c["y"])
            cols[name] = [c["t"].strip() for c in best]
        row_comments = [c["t"].split(":", 1)[-1].strip() for c in comments if top <= c["y"] < bottom]
        rows.append({"page": page["page"], "month": month, "raw": cols, "comments": row_comments,
                     "bbox": [round(top, 4), round(bottom, 4)]})
    return rows


# ---------- field parsers ----------

def parse_address(lines, w):
    lines = [l for l in lines if l]
    zi = None
    for i in range(len(lines) - 1, -1, -1):
        if ZIP_LINE_RE.match(lines[i]):
            zi = i
            break
    out = {"street": None, "city": None, "zip": None, "assessed": None}
    if zi is None:
        w.append("address: no ZIP/value line")
        out["street"] = " ".join(lines)
        return out
    zl = lines[zi]
    m = re.match(r"^(\S{4,5})\s*(.*)$", zl)
    out["zip"] = m.group(1)
    if not re.fullmatch(r"\d{5}", out["zip"]):
        w.append("address: zip unreadable")
    out["assessed"] = money(m.group(2))
    before = lines[:zi]
    if len(before) >= 2:
        out["city"] = before[-1]
        out["street"] = " ".join(before[:-1])
    elif len(before) == 1:
        out["street"] = before[0]
        w.append("address: city missing")
    return out


def parse_class(lines):
    t = " ".join(lines)
    t = re.sub(r"Condominiu\s+m", "Condominium", t)
    t = re.sub(r"Resid\w*", "Residential", t)
    out = {"propertyClass": None, "yearBuilt": None, "units": None}
    t = re.sub(r"\b(1[89]\d\d|20[0-3]\d)[1Il|]\s*$", r"\1/", t)  # "20171" = "2017/"
    m = re.search(r"(\d{4})?\s*/\s*(\d+)?\s*$", t)
    if m:
        out["yearBuilt"] = int(m.group(1)) if m.group(1) else None
        out["units"] = int(m.group(2)) if m.group(2) else None
        t = t[:m.start()]
    else:
        m = re.search(r"\b(1[89]\d\d|20\d\d)\b\s*$", t)
        if m:
            out["yearBuilt"] = int(m.group(1))
            t = t[:m.start()]
    out["propertyClass"] = snap_class(t.strip(" ,|"))
    return out


CLASSES = ["Single Family Residential", "Condominium", "Townhouse", "Multifamily", "Land", "Commercial",
           "Mobile Home", "Duplex", "Industrial", "Vacant Lot", "Farm/Ranch"]


def snap_class(t):
    if not t:
        return None
    if t.upper().startswith("CONDO"):
        return "Condominium"
    best = difflib.get_close_matches(t.title(), CLASSES, n=1, cutoff=0.55)
    return best[0] if best else t


def parse_legal(lines):
    out = {"legal": None, "sqft": None, "beds": None, "baths": None, "garage": None, "pool": None}
    lines = list(lines)
    # last line like "2046 0/2/2 N", "2545 4/3/ N", "4096 /1/ N", "1223 // N", "9976"
    for i in range(len(lines) - 1, -1, -1):
        m = re.search(r"(?:^|\s)(?:(\d+)\s+)?(\d*)/(\d*)/(\d*)\s*([A-Z])?\s*$", lines[i])
        if m and (m.group(1) or m.group(2) or m.group(3)):
            out["sqft"] = int(m.group(1)) if m.group(1) else None
            out["beds"] = int(m.group(2)) if m.group(2) else None
            out["baths"] = int(m.group(3)) if m.group(3) else None
            out["garage"] = int(m.group(4)) if m.group(4) else None
            out["pool"] = m.group(5)
            lines[i] = lines[i][:m.start()].strip()
            break
        m = re.match(r"^(\d{3,6})$", lines[i])
        if m and i == len(lines) - 1:
            out["sqft"] = int(m.group(1))
            lines[i] = ""
            break
    t = " ".join(l.strip(" |") for l in lines if l.strip(" |"))
    t = re.sub(r"^[_T]?[LT]OT:", "LOT:", t)
    out["legal"] = t or None
    return out


def _is_lender(line):
    toks = re.findall(r"[A-Z.]+", line.upper())
    return any(tok in LENDER_WORDS for tok in toks)


def parse_parties(lines, w):
    lines = [l for l in lines if l]
    out = {"mortgagor": None, "mortgagee": None, "trustee": None}
    if not lines:
        return out
    # trustee = from the last line containing a comma
    ti = None
    for i in range(len(lines) - 1, 0, -1):
        if "," in lines[i] or re.match(r"^[A-Z'\-]{3,}\.(\s|$)", lines[i]):
            ti = i
            break
    if ti is not None:
        out["trustee"] = re.sub(r"^([A-Z'\-]{3,})\.", r"\1,", " ".join(lines[ti:]))
        rest = lines[:ti]
    else:
        rest = lines
        w.append("parties: trustee not found")
    # mortgagor ends at "ET AL"
    split = None
    for i, l in enumerate(rest):
        if re.search(r"\bET\s*AL\b", l) or (l.strip() == "AL" and i > 0 and rest[i - 1].rstrip().endswith("ET")):
            split = i + 1
            break
    if split is None:
        for i in range(1, len(rest)):
            if _is_lender(rest[i]):
                # "RGB ENTERPRISES / LLC / HAPPY STATE BANK": a line ending in a company suffix that is
                # followed by another lender line is still the (corporate) mortgagor.
                last = rest[i].split()[-1].upper().strip(".,")
                nxt = rest[i + 1] if i + 1 < len(rest) else ""
                if last in CORP_SUFFIX and nxt and _is_lender(nxt) and nxt.split()[0].upper() not in CORP_SUFFIX:
                    continue
                while (split if split is not None else i) - 1 >= 1:
                    j = split if split is not None else i
                    first = rest[j].split()[0].upper().strip(".,&")
                    if first in CONTINUATION_START and not re.search(r"\bET\s*AL\b|\bAL$", rest[j - 1]) \
                            and not rest[j - 1].split()[-1].upper() in CORP_SUFFIX:
                        split = j - 1
                    else:
                        break
                if split is None:
                    split = i
                break
    if split is None:
        split = max(1, len(rest) - 1)
    out["mortgagor"] = " ".join(rest[:split]) or None
    out["mortgagee"] = " ".join(rest[split:]) or None
    return out


def parse_loan(lines):
    out = {"loanDate": None, "rate": None, "loanType": None}
    rest = []
    for l in lines:
        m = re.search(r"(\d{1,2})/((?:19|20)\d\d)\b", l)
        if out["loanDate"] is None and m:
            mo = m.group(1)
            if int(mo) > 12:  # "13/2007": a stray border stroke read as "1"
                mo = mo[-1]
            out["loanDate"] = f"{int(mo)}/{m.group(2)}"
            l = (l[:m.start()] + l[m.end():]).strip()
        if out["rate"] is None and re.fullmatch(r"\d{1,2}\.\d+|\d{1,2}", l) and l not in ("0",):
            out["rate"] = float(l)
            continue
        if l in ("0/0",) or re.fullmatch(r"\d", l):
            continue
        if l:
            rest.append(l)
    out["loanType"] = snap_loan_type(" ".join(rest))
    return out


def snap_loan_type(t):
    t = t.strip()
    if not t:
        return None
    u = t.upper()
    if (re.search(r"FAN|FRED|FRE[DE]|MA[EC]", u) or (u.startswith("F") and not u.startswith("FHA"))) \
            and not re.search(r"REVERSE|COMMERCIAL|CONV", u):
        return "Fannie Mae / Freddie Mac"
    best = difflib.get_close_matches(t, LOAN_TYPES, n=1, cutoff=0.6)
    if best:
        return best[0]
    for lt in LOAN_TYPES:
        if lt.upper() in u:
            return lt
    return t


def parse_amounts(lines):
    out = {"origAmount": None, "estBalance": None, "docNumber": None}
    moneys = []
    for l in lines:
        if "$" in l or ("," in l and re.search(r"\d", l)):
            moneys.append(money(l))
        elif re.fullmatch(r"[\d/\-]{6,}", l):
            out["docNumber"] = l
    if moneys:
        out["origAmount"] = moneys[0]
    if len(moneys) > 1:
        out["estBalance"] = moneys[1]
    return out


def parse_payment(lines):
    out = {"monthlyPayment": None, "saleTime": None, "maturity": None}
    for l in lines:
        if re.fullmatch(r"\d{1,2}/\d{4}", l):
            out["maturity"] = l
        elif re.fullmatch(r"\d{1,2}\s*-\s*\d{1,2}", l):
            out["saleTime"] = l.replace(" ", "")
        elif re.fullmatch(r"[\d,]+", l) and out["monthlyPayment"] is None:
            out["monthlyPayment"] = int(l.replace(",", ""))
        else:
            # combined line e.g. "610 10-1 10/2045"
            for tok in l.split():
                if re.fullmatch(r"\d{1,2}/\d{4}", tok):
                    out["maturity"] = tok
                elif re.fullmatch(r"\d{1,2}-\d{1,2}", tok):
                    out["saleTime"] = tok
                elif re.fullmatch(r"\d+", tok) and out["monthlyPayment"] is None:
                    out["monthlyPayment"] = int(tok)
    return out


def parse_equity(lines):
    out = {"equity": None, "margin": None}
    num = []
    for l in lines:
        m = re.search(r"(-?\d+)\s*%", l)
        if m:
            out["margin"] = int(m.group(1))
            pre = l[:m.start()].strip()
            if pre:
                num.append(pre)
        else:
            num.append(l)
    if num:
        out["equity"] = money("".join(num).replace(" ", ""))
    return out


def build_row(raw_row, seq):
    w = []
    r = raw_row["raw"]
    row = {"seq": seq}
    ids = [int(x) for x in re.findall(r"\b\d{1,4}\b", " ".join(r["id"]))]
    row["id"] = ids[0] if ids else None
    if row["id"] is None:
        w.append("id missing")
    row.update(parse_address(r["address"], w))
    row.update(parse_class(r["class"]))
    row.update(parse_legal(r["legal"]))
    row.update(parse_parties(r["parties"], w))
    row.update(parse_loan(r["loan"]))
    row.update(parse_amounts(r["amounts"]))
    row.update(parse_payment(r["payment"]))
    row.update(parse_equity(r["equity"]))
    # Sanity checks: the book computes equity = assessed value - estimated balance.
    # The margin % is a short token and OCRs reliably, so use it to decide which number to trust.
    a, bal, eq, mg = row["assessed"], row["estBalance"], row["equity"], row["margin"]
    if a and bal is not None:
        calc = a - bal
        pct = lambda v: 100 * v / a
        calc_ok = mg is not None and abs(pct(calc) - mg) <= 1.5
        ocr_ok = eq is not None and mg is not None and abs(pct(eq) - mg) <= 1.5
        if eq is not None and abs(calc - eq) <= 2:
            pass
        elif calc_ok or not ocr_ok:
            if mg is not None and not calc_ok:
                w.append("numbers disagree: check assessed / balance / equity")
            row["equity"] = calc
        else:
            row["estBalance"] = a - eq
            w.append(f"est. balance OCR {bal} corrected from equity")
        row["margin"] = round(pct(row["equity"]))
    if row["equity"] is None and row["loanType"] != "HOA":
        w.append("equity missing")
    row["comments"] = raw_row["comments"]
    row["page"] = raw_row["page"]
    row["month"] = raw_row["month"]
    row["raw"] = {k: v for k, v in r.items()}
    row["warnings"] = w
    return row


def _fix_trustees(rows):
    """Trustees repeat across a book; recover ones whose comma the OCR dropped."""
    counts = {}
    for r in rows:
        if r["trustee"]:
            counts[r["trustee"]] = counts.get(r["trustee"], 0) + 1
    known = [t for t, n in counts.items() if n >= 2]
    # snap one-off OCR variants ("PPLEGATE, CHRIST") to a trustee seen more often
    by_count = sorted(counts, key=counts.get, reverse=True)
    for r in rows:
        t = r["trustee"]
        if t and counts[t] <= 2:
            for k in by_count:
                if k != t and counts[k] > counts[t] and difflib.SequenceMatcher(None, t, k).ratio() >= 0.8:
                    r["trustee"] = k
                    break
    for r in rows:
        if r["trustee"] or not r["mortgagee"]:
            continue
        words = r["mortgagee"].split()
        for k in range(min(4, len(words) - 1), 1, -1):
            tail = " ".join(words[-k:])
            best = difflib.get_close_matches(tail.replace(",", ""), [t.replace(",", "") for t in known], n=1, cutoff=0.7)
            if best:
                r["trustee"] = next(t for t in known if t.replace(",", "") == best[0])
                r["mortgagee"] = " ".join(words[:-k])
                r["warnings"] = [x for x in r["warnings"] if not x.startswith("parties: trustee")]
                break


def parse_book(ocr_path):
    pages = load_pages(ocr_path)
    raw_rows = []
    meta = {"pages": len(pages), "listPages": []}
    for p in pages:
        if is_list_page(p["lines"]):
            meta["listPages"].append(p["page"])
            raw_rows.extend(parse_page(p))
    rows = [build_row(rr, i + 1) for i, rr in enumerate(raw_rows)]
    _fix_trustees(rows)
    # Fill missing / fix obviously wrong IDs using sequence
    for i, row in enumerate(rows):
        prev = rows[i - 1]["id"] if i > 0 else 0
        if row["id"] is None or (prev is not None and row["id"] != prev + 1 and
                                  i + 1 < len(rows) and rows[i + 1]["id"] == prev + 2):
            row["id"] = (prev or 0) + 1
    months = [r["month"] for r in rows if r["month"]]
    meta["month"] = max(set(months), key=months.count) if months else None
    # County: from page text "<N> Postings in <County> County"
    for p in pages:
        for l in p["lines"]:
            m = re.search(r"(\d+)\s+Postings in (\w[\w ]*?) County", l["t"])
            if m:
                meta["county"] = m.group(2).strip()
                meta["postedCount"] = int(m.group(1))
                break
        if "county" in meta:
            break
    return {"meta": meta, "rows": rows}


if __name__ == "__main__":
    book = parse_book(sys.argv[1])
    if "--dump" in sys.argv:
        m = book["meta"]
        print(f"rows={len(book['rows'])} meta={m}")
        for r in book["rows"]:
            flag = " !! " + "; ".join(r["warnings"]) if r["warnings"] else ""
            print(f"#{r['id']:>3} p{r['page']} {r['street']} | {r['city']} {r['zip']} ${r['assessed']} | "
                  f"{r['propertyClass']} {r['yearBuilt']} | {r['sqft']}sf {r['beds']}/{r['baths']}/{r['garage']} | "
                  f"{r['mortgagor']} / {r['mortgagee']} / {r['trustee']} | {r['loanDate']} {r['rate']} {r['loanType']} | "
                  f"{r['origAmount']} {r['estBalance']} {r['docNumber']} | {r['monthlyPayment']} {r['saleTime']} "
                  f"{r['maturity']} | {r['equity']} {r['margin']}%{flag}")
    else:
        json.dump(book, sys.stdout, indent=1)
