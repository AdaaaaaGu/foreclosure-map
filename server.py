"""Local server for the foreclosure map.

  python3 server.py [port]      (default 8765) — or just ./run.sh

Endpoints
  GET    /api/books                       list saved books (meta only)
  GET    /api/books/<id>                  full book
  DELETE /api/books/<id>
  POST   /api/upload                      body = PDF bytes, header X-Filename -> {job}
  POST   /api/books/<id>/results          body = results PDF / CSV / JSON -> {job}
  PATCH  /api/books/<id>/rows/<seq>       {"soldPrice": 123, "status": "Sold"|"Unsold"|null, "note": "..."}
  GET    /api/jobs/<job>                  progress {stage, done, total, bookId, error}
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import geocode
import parser as P
import results as R

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
DATA = os.path.join(HERE, "data")
BOOKS = os.path.join(DATA, "books")
UPLOADS = os.path.join(DATA, "uploads")
CACHE = os.path.join(DATA, "cache")
OCR_BIN = os.path.join(HERE, "ocr", "pdf_ocr")
OCR_SCALE = "4.2"  # the scans are ~300 dpi A4; 4.2x of 595pt ≈ native resolution

for d in (BOOKS, UPLOADS, CACHE):
    os.makedirs(d, exist_ok=True)

JOBS = {}
BOOK_LOCK = threading.Lock()


def book_path(book_id):
    if not re.fullmatch(r"[\w\-]+", book_id):
        raise ValueError("bad id")
    return os.path.join(BOOKS, book_id + ".json")


def load_book(book_id):
    with open(book_path(book_id)) as f:
        return json.load(f)


def save_book(book):
    p = book_path(book["meta"]["id"])
    with open(p + ".tmp", "w") as f:
        json.dump(book, f)
    os.replace(p + ".tmp", p)


def run_ocr(pdf_path, sha, job, strips=P.OCR_STRIPS, cache_tag="book"):
    """Runs the Vision OCR helper with per-page progress; cached by PDF hash."""
    out = os.path.join(CACHE, f"{sha}.{cache_tag}.jsonl")
    if os.path.exists(out):
        return out
    job.update(stage="Reading pages (OCR)", done=0, total=0)
    tmp = out + ".part"
    args = [OCR_BIN, pdf_path, OCR_SCALE] + ([strips] if strips else [])
    with open(tmp, "w") as f:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            o = json.loads(line)
            if o["type"] == "progress":
                job.update(done=o["page"] - 1, total=o["pages"])
            else:
                f.write(line)
                job.update(done=o["page"])
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError("OCR failed: " + err.strip())
    os.replace(tmp, out)
    return out


def process_book(job, pdf_path, sha, filename):
    try:
        ocr = run_ocr(pdf_path, sha, job)
        job.update(stage="Parsing table", done=0, total=0)
        book = P.parse_book(ocr)
        if not book["rows"]:
            raise RuntimeError("No foreclosure-list table found in this PDF.")
        job.update(stage="Locating addresses", done=0, total=len(book["rows"]))
        geocode.geocode_rows(book["rows"], progress=lambda d, t: job.update(done=d, total=t))
        m = book["meta"]
        county = (m.get("county") or "County").replace(" ", "-")
        month = (m.get("month") or "").replace("/", "-")
        m.update(id=f"{county}-{month}-{sha[:8]}".strip("-"), filename=filename, sha=sha,
                 rowCount=len(book["rows"]), resultsAttached=False, uploadedAt=time.time())
        with BOOK_LOCK:
            save_book(book)
        job.update(stage="Done", bookId=m["id"], finished=True)
    except Exception as e:
        traceback.print_exc()
        job.update(error=str(e), finished=True)


def process_results(job, book_id, blob, filename):
    try:
        name = filename.lower()
        if name.endswith(".pdf") or blob[:4] == b"%PDF":
            sha = hashlib.sha256(blob).hexdigest()
            pdf = os.path.join(UPLOADS, sha + ".pdf")
            with open(pdf, "wb") as f:
                f.write(blob)
            # Results layout is not known yet: OCR full pages only and match by address/ID.
            ocr = run_ocr(pdf, sha, job, strips=None, cache_tag="results")
            job.update(stage="Matching sold prices", done=0, total=0)
            entries = R.parse_results_ocr(ocr)
        elif name.endswith(".json"):
            entries = json.loads(blob.decode())
        else:  # CSV: columns id/address, soldPrice / sold / price
            entries = R.parse_results_csv(blob.decode("utf-8-sig"))
        with BOOK_LOCK:
            book = load_book(book_id)
            stats = R.merge(book, entries, source=filename)
            save_book(book)
        job.update(stage="Done", bookId=book_id, finished=True, stats=stats)
    except Exception as e:
        traceback.print_exc()
        job.update(error=str(e), finished=True)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")  # always pick up edited web files
        super().end_headers()

    def log_message(self, fmt, *args):
        if "/api/jobs/" not in (args[0] if args else ""):
            sys.stderr.write("%s\n" % (fmt % args))

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/books":
                out = []
                for fn in sorted(os.listdir(BOOKS)):
                    if fn.endswith(".json"):
                        with open(os.path.join(BOOKS, fn)) as f:
                            out.append(json.load(f)["meta"])
                out.sort(key=lambda m: m.get("uploadedAt", 0), reverse=True)
                return self.send_json(out)
            m = re.fullmatch(r"/api/books/([\w\-]+)", path)
            if m:
                return self.send_json(load_book(m.group(1)))
            m = re.fullmatch(r"/api/jobs/([\w\-]+)", path)
            if m:
                job = JOBS.get(m.group(1))
                return self.send_json(job or {"error": "unknown job"}, 200 if job else 404)
        except FileNotFoundError:
            return self.send_json({"error": "not found"}, 404)
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/upload":
            blob = self.read_body()
            if blob[:4] != b"%PDF":
                return self.send_json({"error": "Please upload a PDF."}, 400)
            filename = self.headers.get("X-Filename", "book.pdf")
            sha = hashlib.sha256(blob).hexdigest()
            pdf = os.path.join(UPLOADS, sha + ".pdf")
            with open(pdf, "wb") as f:
                f.write(blob)
            job_id = uuid.uuid4().hex[:12]
            JOBS[job_id] = {"stage": "Starting", "done": 0, "total": 0}
            threading.Thread(target=process_book, args=(JOBS[job_id], pdf, sha, filename), daemon=True).start()
            return self.send_json({"job": job_id})
        m = re.fullmatch(r"/api/books/([\w\-]+)/results", path)
        if m:
            blob = self.read_body()
            filename = self.headers.get("X-Filename", "results")
            job_id = uuid.uuid4().hex[:12]
            JOBS[job_id] = {"stage": "Starting", "done": 0, "total": 0}
            threading.Thread(target=process_results, args=(JOBS[job_id], m.group(1), blob, filename),
                             daemon=True).start()
            return self.send_json({"job": job_id})
        self.send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        m = re.fullmatch(r"/api/books/([\w\-]+)/rows/(\d+)", self.path.split("?")[0])
        if not m:
            return self.send_json({"error": "not found"}, 404)
        patch = json.loads(self.read_body() or b"{}")
        with BOOK_LOCK:
            book = load_book(m.group(1))
            row = next((r for r in book["rows"] if r["seq"] == int(m.group(2))), None)
            if not row:
                return self.send_json({"error": "row not found"}, 404)
            for k in ("soldPrice", "status", "note", "favorite"):
                if k in patch:
                    row[k] = patch[k]
            if "soldPrice" in patch:
                row["status"] = "Sold" if patch["soldPrice"] else row.get("status")
            R.refresh_status(book)
            save_book(book)
        self.send_json(row)

    def do_DELETE(self):
        m = re.fullmatch(r"/api/books/([\w\-]+)", self.path.split("?")[0])
        if not m:
            return self.send_json({"error": "not found"}, 404)
        try:
            os.remove(book_path(m.group(1)))
        except FileNotFoundError:
            pass
        self.send_json({"ok": True})


def main():
    if not os.path.exists(OCR_BIN):
        print("Compiling OCR helper…")
        subprocess.check_call(["swiftc", "-O", os.path.join(HERE, "ocr", "pdf_ocr.swift"), "-o", OCR_BIN])
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Foreclosure map running at http://localhost:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
