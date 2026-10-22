"""Geocode parsed rows. Free services, no keys:
  1. US Census batch geocoder (one request for the whole book)
  2. ArcGIS World geocoder (per address) for Census misses (new subdivisions, OCR-mangled streets)
  3. ZIP centroid via Nominatim  -> flagged "approximate"
Results are cached in data/geocache.json, keyed by the normalized address.
"""
import csv
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "data", "geocache.json")
UA = "foreclosure-map-local/1.0"
_lock = threading.Lock()


def _load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_PATH)


def clean_street(street):
    s = (street or "").upper()
    s = re.sub(r"\s+(UNIT|APT|#|STE|BLDG)\s*\S+$", "", s)  # units confuse geocoders
    s = re.sub(r"[^A-Z0-9 \-/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def key_for(row):
    return f"{clean_street(row.get('street'))}|{(row.get('city') or '').upper()}|{row.get('zip') or ''}"


def _http(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def census_batch(items):
    """items: list of (id, street, city, zip) -> {id: (lat, lon)}"""
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, street, city, zp in items:
        w.writerow([i, street, city, "TX", zp])
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\nPublic_AR_Current\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; filename=\"a.csv\"\r\n"
        f"Content-Type: text/csv\r\n\r\n{buf.getvalue()}\r\n--{boundary}--\r\n"
    ).encode()
    text = _http("https://geocoding.geo.census.gov/geocoder/locations/addressbatch", data=body,
                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
    out = {}
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) >= 6 and rec[2] == "Match":
            lon, lat = rec[5].split(",")
            out[rec[0]] = (float(lat), float(lon), rec[4])
    return out


def arcgis(street, city, zp):
    q = f"{street}, {city or ''}, TX {zp or ''}"
    url = ("https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?"
           + urllib.parse.urlencode({"f": "json", "maxLocations": 1, "outFields": "Addr_type,Score",
                                     "SingleLine": q, "countryCode": "USA"}))
    data = json.loads(_http(url, timeout=30))
    c = (data.get("candidates") or [None])[0]
    if not c or c.get("score", 0) < 80:
        return None
    t = c.get("attributes", {}).get("Addr_type", "")
    exact = t in ("PointAddress", "Subaddress", "StreetAddress", "StreetAddressExt")
    return c["location"]["y"], c["location"]["x"], c.get("address"), exact


def zip_centroid(zp):
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"format": "json", "limit": 1, "postalcode": zp, "country": "us"}))
    data = json.loads(_http(url, timeout=30))
    time.sleep(1)  # Nominatim usage policy: max 1 request/second
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"])
    return None


def geocode_rows(rows, progress=None):
    """Adds lat, lon, geoMatch ('exact'|'approximate'|None), geoAddress to each row in place."""
    with _lock:
        cache = _load_cache()
    todo = []
    for r in rows:
        k = key_for(r)
        if k in cache:
            r.update(cache[k])
        else:
            todo.append(r)
    total = len(rows)
    done = total - len(todo)
    if progress:
        progress(done, total)

    if todo:
        items = [(str(i), clean_street(r["street"]), r.get("city") or "", r.get("zip") or "")
                 for i, r in enumerate(todo)]
        try:
            hits = census_batch(items)
        except Exception as e:  # network trouble -> fall through to per-address lookups
            print("census batch failed:", e)
            hits = {}
        for i, r in enumerate(todo):
            res = None
            if str(i) in hits:
                lat, lon, addr = hits[str(i)]
                res = {"lat": lat, "lon": lon, "geoMatch": "exact", "geoAddress": addr, "geoSource": "census"}
            else:
                try:
                    a = arcgis(clean_street(r["street"]), r.get("city"), r.get("zip"))
                except Exception as e:
                    print("arcgis failed:", e)
                    a = None
                if a:
                    lat, lon, addr, exact = a
                    res = {"lat": lat, "lon": lon, "geoMatch": "exact" if exact else "approximate",
                           "geoAddress": addr, "geoSource": "arcgis"}
                elif r.get("zip") and re.fullmatch(r"\d{5}", r["zip"]):
                    try:
                        z = zip_centroid(r["zip"])
                    except Exception:
                        z = None
                    if z:
                        res = {"lat": z[0], "lon": z[1], "geoMatch": "approximate",
                               "geoAddress": f"ZIP {r['zip']} (center)", "geoSource": "zip"}
            if res is None:
                res = {"lat": None, "lon": None, "geoMatch": None, "geoAddress": None, "geoSource": None}
            r.update(res)
            if res["lat"] is not None:
                cache[key_for(r)] = res
            done += 1
            if progress:
                progress(done, total)
        with _lock:
            merged = _load_cache()
            merged.update(cache)
            _save_cache(merged)
    return rows


if __name__ == "__main__":
    import sys
    import parser as P
    book = P.parse_book(sys.argv[1])
    geocode_rows(book["rows"], progress=lambda d, t: print(f"\rgeocoding {d}/{t}", end=""))
    print()
    from collections import Counter
    print(Counter((r["geoMatch"], r["geoSource"]) for r in book["rows"]))
    for r in book["rows"]:
        if r["geoMatch"] != "exact":
            print(r["id"], r["street"], r["city"], r["zip"], "->", r["geoMatch"], r["geoAddress"])
