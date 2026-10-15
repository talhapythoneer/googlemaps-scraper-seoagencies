"""
STEP 1 - Google Maps collector.

Builds a city list (GeoNames, population >= --min-pop, CH limited to German-speaking
areas) and searches Google Maps for every keyword x city via Maps' internal JSON
search endpoint (no browser). Every query's results are appended to
output/step1_raw.jsonl so the run can be stopped and resumed at any time.
At the end all listings are deduplicated by Google place id and written to
output/step1_maps.csv (input for step 2).

Examples
  python step1_google_maps.py --cities "München,Berlin" --keywords "SEO Agentur"   # quick test
  python step1_google_maps.py --countries DE --max-cities 30                        # test batch
  python step1_google_maps.py                                                      # full run
  python step1_google_maps.py --export-only                                        # rebuild CSV from raw
"""
import argparse
import io
import json
import os
import random
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus

from curl_cffi import requests
from tqdm import tqdm

import common as C

RAW_JSONL = os.path.join(C.OUTPUT_DIR, "step1_raw.jsonl")
EXCLUDED_CSV = os.path.join(C.OUTPUT_DIR, "step1_excluded.csv")
GEONAMES_URL = "https://download.geonames.org/export/dump/{name}.zip"

STEP1_FIELDS = [
    "place_id", "cid", "data_id", "name", "categories", "primary_category", "website", "domain",
    "phone", "phone_international", "address", "street", "postcode", "city", "locality",
    "country_code", "country", "ch_language", "address_hidden", "rating", "reviews", "latitude", "longitude",
    "claimed", "opening_hours", "maps_url", "found_by_keywords", "found_in_cities", "hit_count",
    "freelancer_score_name", "freelancer_label_name", "freelancer_reasons_name",
]


# --------------------------------------------------------------------------
# City list
# --------------------------------------------------------------------------
def geonames_file_for(min_pop):
    if min_pop >= 15000:
        return "cities15000"
    if min_pop >= 5000:
        return "cities5000"
    if min_pop >= 1000:
        return "cities1000"
    return "cities500"


def build_city_list(min_pop, countries, include_districts=True, refresh=False):
    """Returns list of dicts and caches them in data/cities_<countries>_<minpop>.csv (editable)."""
    C.ensure_dirs()
    tag = "-".join(sorted(countries))
    path = os.path.join(C.DATA_DIR, f"cities_{tag}_{min_pop}{'' if include_districts else '_nodistricts'}.csv")
    if os.path.exists(path) and not refresh:
        return C.read_csv(path), path

    fname = geonames_file_for(min_pop)
    zpath = os.path.join(C.DATA_DIR, f"{fname}.zip")
    if not os.path.exists(zpath) or refresh:
        print(f"Downloading GeoNames {fname} ...")
        r = requests.get(GEONAMES_URL.format(name=fname), timeout=120)
        r.raise_for_status()
        with open(zpath, "wb") as f:
            f.write(r.content)
    with zipfile.ZipFile(zpath) as z:
        txt = z.read(f"{fname}.txt").decode("utf-8")

    rows = []
    for line in txt.splitlines():
        p = line.split("\t")
        if len(p) < 15:
            continue
        cc, admin1, fcode = p[8], p[10], p[7]
        pop = int(p[14] or 0)
        if cc not in countries or pop < min_pop:
            continue
        if fcode == "PPLX" and not include_districts:
            continue
        if cc == "CH" and (admin1 not in C.CH_GERMAN_CANTONS or p[1] in C.CH_NON_GERMAN_TOWNS
                           or p[2] in C.CH_NON_GERMAN_TOWNS):
            continue
        rows.append({
            "geonameid": p[0], "name": p[1], "country_code": cc, "admin1": admin1,
            "population": pop, "latitude": p[4], "longitude": p[5], "feature_code": fcode,
        })
    # German names for the search query (GeoNames uses "Munich", "Vienna", "Nuremberg" ...)
    de_names = german_names({r["geonameid"] for r in rows}, countries, refresh)
    for r in rows:
        alt = de_names.get(r["geonameid"])
        keep = (not alt or alt.lower() in r["name"].lower() or r["name"] in GERMAN_NAME_KEEP)
        r["query_name"] = r["name"] if keep else alt
    rows.sort(key=lambda r: (-int(r["population"])))
    C.write_csv(path, rows, ["geonameid", "name", "query_name", "country_code", "admin1", "population",
                             "latitude", "longitude", "feature_code"])
    return rows, path


GERMAN_NAME_KEEP = {"Fribourg"}  # German name would be ambiguous (Freiburg im Breisgau)


def german_names(ids, countries, refresh=False):
    """geonameid -> preferred German name from GeoNames alternatenames/<CC>.zip."""
    out = {}
    for cc in countries:
        zpath = os.path.join(C.DATA_DIR, f"alternatenames_{cc}.zip")
        if not os.path.exists(zpath) or refresh:
            r = requests.get(f"https://download.geonames.org/export/dump/alternatenames/{cc}.zip", timeout=120)
            r.raise_for_status()
            with open(zpath, "wb") as f:
                f.write(r.content)
        with zipfile.ZipFile(zpath) as z:
            for line in z.read(f"{cc}.txt").decode("utf-8").splitlines():
                p = line.split("\t")
                # id, geonameid, lang, name, preferred, short, colloquial, historic
                if len(p) > 7 and p[1] in ids and p[2] == "de" and p[4] == "1" and p[6] != "1" and p[7] != "1":
                    out.setdefault(p[1], p[3])
    return out


# --------------------------------------------------------------------------
# Google Maps endpoint
# --------------------------------------------------------------------------
class Blocked(Exception):
    pass


_local = threading.local()


def _session(proxy):
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session(impersonate="chrome")
        _local.s = s
    s.proxies = {"http": proxy, "https": proxy} if proxy else {}
    return s


def viewport_m(pop, fcode):
    """Approximate map viewport (metres) for a place."""
    if fcode == "PPLX":
        return 6000
    pop = int(pop or 0)
    if pop >= 500000:
        return 30000
    if pop >= 100000:
        return 20000
    if pop >= 20000:
        return 12000
    return 9000


def maps_search_page(query, lat, lng, offset, dist, gl, proxy=None, timeout=30):
    pb = (f"!4m8!1m3!1d{dist}!2d{lng}!3d{lat}!3m2!1i1024!2i768!4f13.1"
          f"!7i20!8i{offset}!10b1")
    url = (f"https://www.google.com/search?tbm=map&authuser=0&hl=de&gl={gl}"
           f"&q={quote_plus(query)}&pb={quote_plus(pb, safe='!')}")
    r = _session(proxy).get(url, timeout=timeout)
    body = r.text
    if r.status_code == 429 or "/sorry/" in str(r.url) or (r.status_code != 200):
        raise Blocked(f"HTTP {r.status_code} {str(r.url)[:80]}")
    if not body.startswith(")]}'"):
        raise Blocked("unexpected body (captcha?)")
    body = body[body.index("\n") + 1:].rstrip()
    if body.endswith('/*""*/'):
        body = body[:-6]
    data = json.loads(body)
    items = []
    try:
        items = data[0][1] or []
    except (IndexError, TypeError):
        pass
    return [it[14] for it in items if isinstance(it, list) and len(it) > 14 and it[14]]


def _g(a, *idx):
    """Safe nested get."""
    for i in idx:
        if not isinstance(a, list) or i >= len(a) or a[i] is None:
            return None
        a = a[i]
    return a


def parse_place(p):
    data_id = _g(p, 10) or ""
    cid = ""
    m = re.match(r"0x[0-9a-f]+:0x([0-9a-f]+)", data_id)
    if m:
        cid = str(int(m.group(1), 16))
    place_id = _g(p, 78) or ""
    website = C.clean_maps_url(_g(p, 7, 0) or "") if _g(p, 7, 0) else ""

    phone_nat, phone_int = _g(p, 178, 0, 0) or "", ""
    for alt in _g(p, 178, 0, 1) or []:
        if isinstance(alt, list) and len(alt) > 1 and alt[1] == 2:
            phone_int = alt[0]

    parts = _g(p, 183, 1) or []
    street = _g(parts, 1) or _g(p, 2, 0) or ""
    city = _g(parts, 3) or ""
    postcode = _g(parts, 4) or ""
    cc = _g(parts, 6) or _g(p, 243) or ""
    locality = ""
    addr_lines = _g(p, 2) or []
    if addr_lines:
        m2 = re.match(r"^(?:[A-Z]{1,2}-)?(\d{4,5})\s+(.+)$", addr_lines[-1] or "")
        if m2:
            postcode = postcode or m2.group(1)
            locality = m2.group(2)
    if not city and locality:
        city = re.sub(r"^Bezirk\s+", "", locality)
        if cc == "DE" and postcode.isdigit() and 10115 <= int(postcode) <= 14199 and locality.startswith("Bezirk"):
            city = "Berlin"
    city = city or _g(p, 166) or ""

    hours = []
    for d in _g(p, 203, 0) or []:
        try:
            hours.append(f"{d[0]}: {', '.join(x[0] for x in d[3])}")
        except (TypeError, IndexError):
            pass

    cats = _g(p, 13) or []
    name = _g(p, 11) or ""
    return {
        "place_id": place_id,
        "cid": cid,
        "data_id": data_id,
        "name": name,
        "categories": ", ".join(cats),
        "primary_category": cats[0] if cats else "",
        "website": website,
        "domain": C.get_domain(website),
        "phone": phone_nat,
        "phone_international": phone_int,
        "address": _g(p, 39) or "",
        "street": street,
        "postcode": postcode,
        "city": city,
        "locality": locality,
        "country_code": cc,
        "rating": _g(p, 4, 7) or "",
        "reviews": _g(p, 4, 8) or "",
        "latitude": _g(p, 9, 2) or "",
        "longitude": _g(p, 9, 3) or "",
        "claimed": "no" if _g(p, 49) else "yes",
        "opening_hours": "; ".join(hours),
        "maps_url": f"https://www.google.com/maps?cid={cid}" if cid else
                    (f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else ""),
    }


def run_query(keyword, city, max_pages, delay, proxies, seen=None, retries=4):
    """seen: shared set of place ids; paging stops once a page brings nothing new (saves requests)."""
    q = f"{keyword} {city.get('query_name') or city['name']}"
    gl = city["country_code"].lower()
    dist = viewport_m(city["population"], city["feature_code"])
    places = []
    for page in range(max_pages):
        for attempt in range(retries + 1):
            proxy = random.choice(proxies) if proxies else None
            try:
                res = maps_search_page(q, city["latitude"], city["longitude"], page * 20, dist, gl, proxy)
                break
            except Blocked:
                if attempt == retries:
                    raise
                time.sleep(min(300, 20 * 2 ** attempt) + random.uniform(0, 5))
            except Exception:  # network / json glitches
                if attempt == retries:
                    raise
                time.sleep(3 + 3 * attempt)
        parsed = [parse_place(p) for p in res]
        places += parsed
        time.sleep(delay + random.uniform(0, delay))
        if len(res) < 20:
            break
        if seen is not None:
            ids = {p["place_id"] or p["data_id"] for p in parsed}
            new = ids - seen
            seen.update(ids)
            if page > 0 and not new:
                break
    return q, places




# --------------------------------------------------------------------------
# Aggregation (incremental: raw results are streamed, only unique places kept in memory)
# --------------------------------------------------------------------------
class Aggregator:
    def __init__(self):
        self.by_id = {}
        self.done = set()
        self.lock = threading.Lock()

    def add(self, rec):
        with self.lock:
            self.done.add(rec["key"])
            for pl in rec["places"]:
                pid = pl.get("place_id") or pl.get("data_id") or (pl["name"] + pl["address"])
                row = self.by_id.get(pid)
                if row is None:
                    row = dict(pl)
                    row["_kw"], row["_cities"], row["hit_count"] = [], [], 0
                    # service-area businesses hide their address -> fall back to the searched city
                    row["address_hidden"] = "" if pl.get("country_code") else "yes"
                    if not pl.get("country_code"):
                        row["country_code"] = rec.get("country_code", "")
                        row["city"] = row.get("city") or rec["city"]
                    self.by_id[pid] = row
                row["hit_count"] += 1
                if rec["keyword"] not in row["_kw"]:
                    row["_kw"].append(rec["keyword"])
                if rec["city"] not in row["_cities"] and len(row["_cities"]) < 30:
                    row["_cities"].append(rec["city"])

    def seen_ids(self):
        with self.lock:
            return set(self.by_id)


def load_raw(agg):
    if not os.path.exists(RAW_JSONL):
        return
    with open(RAW_JSONL, encoding="utf-8") as f:
        for line in f:
            try:
                agg.add(json.loads(line))
            except (json.JSONDecodeError, KeyError):
                continue


def export(agg, allowed_countries, quiet=False):
    with agg.lock:
        rows = [dict(r) for r in agg.by_id.values()]
    kept, excluded = [], []
    for row in rows:
        row["found_by_keywords"] = ", ".join(row.pop("_kw"))
        cities = row.pop("_cities")
        row["found_in_cities"] = ", ".join(cities[:10]) + (f" (+{len(cities) - 10})" if len(cities) > 10 else "")
        cc = row.get("country_code", "")
        row["country"] = C.COUNTRIES.get(cc, cc)
        row["ch_language"] = C.ch_language_region(row["postcode"]) if cc == "CH" else ""
        s, lab, why = C.freelancer_score(row["name"])
        row["freelancer_score_name"], row["freelancer_label_name"], row["freelancer_reasons_name"] = s, lab, why
        if cc not in allowed_countries:
            row["exclude_reason"] = f"country {cc or '?'}"
            excluded.append(row)
        elif cc == "CH" and row["ch_language"] in ("fr", "it"):
            row["exclude_reason"] = f"CH {row['ch_language']}-speaking region"
            excluded.append(row)
        else:
            kept.append(row)
    kept.sort(key=lambda r: (r["country_code"], r["city"], r["name"]))
    C.write_csv(C.STEP1_CSV, kept, STEP1_FIELDS)
    C.write_csv(EXCLUDED_CSV, excluded, STEP1_FIELDS + ["exclude_reason"])
    with_site = sum(1 for r in kept if r["website"])
    domains = len({r["domain"] for r in kept if r["domain"]})
    msg = (f"Step 1 export: {len(kept)} listings ({with_site} with website, {domains} unique domains), "
           f"{len(excluded)} excluded -> {C.STEP1_CSV}")
    (tqdm.write if quiet else print)(msg)


# --------------------------------------------------------------------------
def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--countries", default="DE,AT,CH", help="comma list of DE,AT,CH")
    ap.add_argument("--min-pop", type=int, default=1000, help="minimum population for the city list")
    ap.add_argument("--no-districts", action="store_true", help="skip city districts (GeoNames PPLX)")
    ap.add_argument("--keywords", default=None,
                    help="comma list (used for all places); default = common.MAPS_SEARCH_KEYWORDS with "
                         "per-keyword minimum population")
    ap.add_argument("--cities", default=None, help="comma list of city names to restrict to (test runs)")
    ap.add_argument("--max-cities", type=int, default=0, help="only the N largest cities (test runs)")
    ap.add_argument("--max-pages", type=int, default=6, help="pages of 20 per query (Maps caps ~120)")
    ap.add_argument("--workers", type=int, default=16, help="parallel queries")
    ap.add_argument("--delay", type=float, default=0.3, help="base delay (s) between page requests per worker")
    ap.add_argument("--proxies", default=None, help="file with one proxy URL per line (optional)")
    ap.add_argument("--refresh-cities", action="store_true", help="re-download / rebuild the city list")
    ap.add_argument("--full-paging", action="store_true",
                    help="always fetch all pages (default: stop when a page has only already-seen places)")
    ap.add_argument("--export-every", type=int, default=900, help="re-export step1_maps.csv every N seconds")
    ap.add_argument("--export-only", action="store_true", help="only rebuild step1_maps.csv from raw results")
    args = ap.parse_args()

    C.ensure_dirs()
    countries = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    agg = Aggregator()
    load_raw(agg)
    if args.export_only:
        export(agg, countries)
        return

    cities, cpath = build_city_list(args.min_pop, countries, not args.no_districts, args.refresh_cities)
    if args.cities:
        wanted = [c.strip().lower() for c in args.cities.split(",")]
        cities = [c for c in cities if c["name"].lower() in wanted or c.get("query_name", "").lower() in wanted]
        seen_names, uniq = set(), []          # keep only the most populous match per requested name
        for c in cities:
            if c["name"].lower() not in seen_names:
                seen_names.add(c["name"].lower())
                uniq.append(c)
        cities = uniq
    if args.max_cities:
        cities = cities[: args.max_cities]
    if args.keywords:
        keywords = [(k.strip(), 0) for k in args.keywords.split(",") if k.strip()]
    else:
        keywords = list(C.MAPS_SEARCH_KEYWORDS)
    proxies = []
    if args.proxies:
        with open(args.proxies, encoding="utf-8") as f:
            proxies = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    # job order: keyword priority first, big places first -> the most relevant data lands early
    jobs, total = [], 0
    for ki, (kw, min_pop) in enumerate(keywords):
        for city in cities:
            if int(city["population"]) < min_pop:
                continue
            total += 1
            key = f"{kw}|{city['geonameid']}"
            if key not in agg.done:
                jobs.append((key, kw, city))
    print(f"Cities: {len(cities)} (list: {cpath}) | keywords: {len(keywords)} | queries total: {total} | "
          f"already done: {total - len(jobs)} | to run: {len(jobs)}")

    seen = agg.seen_ids()
    out = open(RAW_JSONL, "a", encoding="utf-8")
    out_lock = threading.Lock()
    pause_until = [0.0]
    last_export = [time.time()]

    def work(job):
        key, kw, city = job
        while time.time() < pause_until[0]:
            time.sleep(5)
        q, places = run_query(kw, city, args.max_pages, args.delay, proxies,
                              None if args.full_paging else seen)
        rec = {"key": key, "keyword": kw, "city": city.get("query_name") or city["name"],
               "country_code": city["country_code"], "query": q, "ts": int(time.time()), "places": places}
        with out_lock:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
        agg.add(rec)
        return len(places)

    failures, failed_total = 0, 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(work, j): j for j in jobs}
            with tqdm(total=len(futs), desc="maps queries", unit="q", mininterval=30) as bar:
                for fut in as_completed(futs):
                    try:
                        fut.result()
                        failures = 0
                    except Exception as e:  # noqa: BLE001
                        failures += 1
                        failed_total += 1
                        tqdm.write(f"  ! {futs[fut][0]}: {type(e).__name__} {str(e)[:120]}")
                        if failures >= 10:
                            # probably blocked: pause everybody for 15 min, failed queries are retried on re-run
                            tqdm.write(f"{failures} consecutive failures - pausing 15 min "
                                       f"({time.strftime('%H:%M')})")
                            pause_until[0] = time.time() + 900
                            failures = 0
                    bar.update(1)
                    if time.time() - last_export[0] > args.export_every:
                        last_export[0] = time.time()
                        try:
                            export(agg, countries, quiet=True)
                        except Exception as e:  # noqa: BLE001
                            tqdm.write(f"  periodic export failed ({e}) - will retry next time")
    except KeyboardInterrupt:
        print("\nInterrupted - progress saved, re-run to resume.")
    finally:
        out.close()
        export(agg, countries)
        if failed_total:
            print(f"{failed_total} queries failed - run the same command again to retry them.")


if __name__ == "__main__":
    main()
