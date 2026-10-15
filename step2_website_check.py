"""
STEP 2 - Website check with curl_cffi (fast, no browser).

Reads output/step1_maps.csv, deduplicates by domain (one row per agency) and for
each website fetches: homepage, up to N service pages, about page, Impressum and
(if needed) the contact page. Extracts:
  keywords found (+ evidence URLs), top priority (link building terms),
  platform flag (Backlinks kaufen / Marktplatz für Backlinks / Publisher-Netzwerk),
  general email, phone, managing director (Impressum), freelancer score.

Rows where the fetch failed / was blocked / no keyword was found are marked
needs_browser=yes and are re-checked with Selenium in step 3.
Results are appended as they finish -> safe to stop and re-run (resumes).

Examples
  python step2_website_check.py --limit 50          # test
  python step2_website_check.py --workers 24        # full run
  python step2_website_check.py --retry-failed      # forget rows that errored, then run again to re-check
"""
import argparse
import glob
import os
import re
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests
from tqdm import tqdm

import common as C

warnings.filterwarnings("ignore")

STEP2_FIELDS_SITE = [
    "final_url", "http_status", "fetch_status", "fetch_error", "pages_checked", "homepage_text_len",
    "keywords_found", "keyword_count", "top_priority", "linkbuilding_keywords", "keyword_evidence_urls",
    "platform_flag", "platform_phrases", "platform_context", "general_email", "other_emails", "site_phone",
    "managing_director", "md_label", "impressum_url",
    "freelancer_score", "freelancer_label", "freelancer_reasons",
]
STEP2_FIELDS = (
    ["domain", "name", "website", "country_code", "country", "city", "postcode", "address", "street",
     "phone", "phone_international", "categories", "primary_category", "rating", "reviews",
     "maps_url", "place_id", "cid", "claimed", "address_hidden", "ch_language", "latitude", "longitude",
     "found_by_keywords", "found_in_cities", "hit_count", "maps_listings", "other_listing_names",
     "freelancer_score_name", "website_type"]
    + STEP2_FIELDS_SITE + ["needs_browser", "checked_by"]
)

_local = threading.local()
MAX_BYTES = 4_000_000


def make_fetch(timeout):
    def fetch(url):
        s = getattr(_local, "s", None)
        if s is None:
            s = requests.Session(impersonate="chrome")
            s.headers.update({"Accept-Language": "de-DE,de;q=0.9,en;q=0.6"})
            _local.s = s
        for attempt in range(3):   # DNS lookups fail spuriously when many threads resolve at once
            try:
                r = s.get(url, timeout=timeout, allow_redirects=True, max_redirects=8)
                break
            except Exception as e:  # noqa: BLE001
                if "curl: (6)" in str(e) and attempt < 2:
                    time.sleep(3 + 5 * attempt)
                elif "SSL" in str(e) or "certificate" in str(e).lower() or "curl: (60)" in str(e):
                    r = s.get(url, timeout=timeout, allow_redirects=True, max_redirects=8, verify=False)
                    break
                else:
                    raise
        ctype = r.headers.get("content-type", "")
        if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return r.status_code, str(r.url), ""
        content = r.content[:MAX_BYTES]
        enc = r.encoding or "utf-8"
        try:
            html = content.decode(enc, errors="replace")
        except LookupError:
            html = content.decode("utf-8", errors="replace")
        if "�" in html[:5000] and enc.lower() != "utf-8":
            html = content.decode("utf-8", errors="replace")
        return r.status_code, str(r.url), html
    return fetch


def group_by_domain(rows):
    """One row per domain: keep the listing with most reviews, remember the others."""
    groups = {}
    for r in rows:
        d = r.get("domain") or ""
        key = d if d else f"nodomain:{r.get('place_id') or r.get('name')}"
        groups.setdefault(key, []).append(r)
    out = []
    for key, rs in groups.items():
        rs.sort(key=lambda r: -int(float(r.get("reviews") or 0)))
        best = dict(rs[0])
        best["domain"] = best.get("domain") or ""
        best["maps_listings"] = len(rs)
        best["other_listing_names"] = " | ".join(dict.fromkeys(x["name"] for x in rs[1:] if x["name"] != best["name"]))[:500]
        kws, cities = [], []
        for x in rs:
            kws += [k.strip() for k in (x.get("found_by_keywords") or "").split(",") if k.strip()]
            cities += [c.strip() for c in (x.get("found_in_cities") or "").split(",") if c.strip()]
        best["found_by_keywords"] = ", ".join(dict.fromkeys(kws))
        best["found_in_cities"] = ", ".join(list(dict.fromkeys(cities))[:15])
        best["hit_count"] = sum(int(x.get("hit_count") or 1) for x in rs)
        if not best["domain"]:
            best["website_type"] = "none"
        elif C.is_social_or_platform(best["domain"]):
            best["website_type"] = "social/platform"
        else:
            best["website_type"] = "website"
        best["_key"] = key
        out.append(best)
    return out


def check_one(row, fetch, max_service_pages):
    out = {k: row.get(k, "") for k in STEP2_FIELDS}
    out["checked_by"] = "curl_cffi"
    if row["website_type"] != "website":
        out["fetch_status"] = "no_website" if row["website_type"] == "none" else "social_only"
        s, lab, why = C.freelancer_score(row["name"])
        out.update({"freelancer_score": s, "freelancer_label": lab, "freelancer_reasons": why,
                    "needs_browser": ""})
        return out
    res = C.crawl_site(row["website"], fetch, name=row["name"], max_service_pages=max_service_pages)
    if res["freelancer_score"] == "":   # site not readable -> name-only score
        res["freelancer_score"], res["freelancer_label"], res["freelancer_reasons"] = C.freelancer_score(row["name"])
    out.update(res)
    # Homepage fetched fine but nothing found: maybe JS-rendered -> let Selenium try
    out["needs_browser"] = "yes" if (res["fetch_status"] != "ok" or not res["keywords_found"]) else ""
    return out


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=C.STEP1_CSV)
    ap.add_argument("--output", default=C.STEP2_CSV)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--max-service-pages", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="check at most N more domains in this run (test)")
    ap.add_argument("--countries", default=None, help="only these country codes, e.g. DE,CH")
    ap.add_argument("--shard", default=None,
                    help="k/N: only domains of shard k of N, written to step2_websites.part<k>.csv "
                         "(run N processes in parallel - HTML parsing is CPU bound)")
    ap.add_argument("--retry-failed", action="store_true", help="remove errored rows from all step 2 files and exit; the next run re-checks them")
    args = ap.parse_args()

    C.ensure_dirs()
    rows = C.read_csv(args.input)
    if not rows:
        raise SystemExit(f"No input rows in {args.input} - run step 1 first.")
    agencies = group_by_domain(rows)
    print(f"Step 1 listings: {len(rows)} -> unique agencies (by domain): {len(agencies)}")

    shard = None
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        shard = (k, n)
        if args.output == C.STEP2_CSV:
            args.output = C.step2_part_path(k)
    if args.retry_failed:
        # Drop errored rows from the main file and every shard part, so the next run re-checks them.
        # Done once up front and then exit, because shards running in parallel share these files.
        dropped = 0
        for path in [C.STEP2_CSV] + sorted(glob.glob(C.step2_part_path("*"))):
            rs = C.read_csv(path)
            keep = [r for r in rs if r.get("fetch_status") != "error"]
            if len(keep) < len(rs):
                C.write_csv(path, keep, STEP2_FIELDS)
                dropped += len(rs) - len(keep)
        raise SystemExit(f"--retry-failed: removed {dropped} errored rows. Run step 2 again to re-check them.")
    existing = C.read_csv(args.output)
    all_done = C.read_step2() if args.output.startswith(os.path.join(C.OUTPUT_DIR, "step2_websites")) else existing
    done = {(r.get("domain") or f"nodomain:{r.get('place_id') or r.get('name')}") for r in all_done + existing}
    todo = [a for a in agencies if a["_key"] not in done]
    if args.countries:
        wanted = {c.strip().upper() for c in args.countries.split(",")}
        todo = [a for a in todo if a.get("country_code") in wanted]
    if shard:
        import zlib
        todo = [a for a in todo if zlib.crc32(a["_key"].encode("utf-8")) % shard[1] == shard[0]]
    if args.limit:
        todo = todo[: args.limit]
    print(f"Already checked: {len(done)} | to check now: {len(todo)}")

    fetch = make_fetch(args.timeout)
    app = C.CsvAppender(args.output, STEP2_FIELDS)
    lock = threading.Lock()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(check_one, a, fetch, args.max_service_pages): a for a in todo}
            for fut in tqdm(as_completed(futs), total=len(futs), desc="websites", unit="site"):
                a = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = {k: a.get(k, "") for k in STEP2_FIELDS}
                    res.update({"fetch_status": "error", "fetch_error": f"{type(e).__name__}: {e}"[:200],
                                "needs_browser": "yes", "checked_by": "curl_cffi"})
                with lock:
                    app.write(res)
    except KeyboardInterrupt:
        print("\nInterrupted - progress saved, re-run to resume.")
    finally:
        app.close()

    final = C.read_step2()
    n = len(final)
    ok = sum(1 for r in final if r["fetch_status"] == "ok")
    kw = sum(1 for r in final if r["keywords_found"])
    top = sum(1 for r in final if r["top_priority"])
    nb = sum(1 for r in final if C.truthy(r["needs_browser"]))
    em = sum(1 for r in final if r["general_email"])
    md = sum(1 for r in final if r["managing_director"])
    print(f"\nStep 2: {n} agencies | fetched ok {ok} | keyword match {kw} (top priority {top}) | "
          f"general email {em} | managing director {md} | needs browser (step 3) {nb}\n-> {args.output}")


if __name__ == "__main__":
    main()
