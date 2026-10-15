"""
STEP 3 - Selenium re-check + final export.

Reads output/step2_websites.csv and re-checks every site marked needs_browser=yes
(no keyword found, blocked, timeout, JS-rendered) with a real headless Chrome,
running the exact same analysis as step 2 on the fully rendered pages.
Browser results are appended to output/step3_browser.csv (resumable), then merged
with step 2 into:
  output/step3_final.csv           all agencies, one row per domain
  output/seo_agencies_final.xlsx   client sheet: Agencies / Not qualified / Info

Examples
  python step3_selenium_check.py --limit 20            # test
  python step3_selenium_check.py --workers 3           # full run
  python step3_selenium_check.py --export-only         # just rebuild the final files
  python step3_selenium_check.py --show                # visible browser (debug)
"""
import argparse
import os
import queue
import re
import threading
import time

import psutil
from tqdm import tqdm

import common as C

BROWSER_CSV = os.path.join(C.OUTPUT_DIR, "step3_browser.csv")
SITE_FIELDS = [
    "final_url", "http_status", "fetch_status", "fetch_error", "pages_checked", "homepage_text_len",
    "keywords_found", "keyword_count", "top_priority", "linkbuilding_keywords", "keyword_evidence_urls",
    "platform_flag", "platform_phrases", "platform_context", "general_email", "other_emails", "site_phone",
    "managing_director", "md_label", "impressum_url",
    "freelancer_score", "freelancer_label", "freelancer_reasons",
]
BROWSER_FIELDS = ["domain"] + SITE_FIELDS

# client-facing columns: (sheet header, internal field)
FINAL_COLUMNS = [
    ("Company name", "name"),
    ("Website", "website"),
    ("Domain", "domain"),
    ("Country", "country"),
    ("City", "city"),
    ("Postcode", "postcode"),
    ("Address", "address"),
    ("General email", "general_email"),
    ("Other emails", "other_emails"),
    ("Phone", "phone_final"),
    ("Managing director", "managing_director"),
    ("MD source (Impressum label)", "md_label"),
    ("Keywords found", "keywords_found"),
    ("Keyword count", "keyword_count"),
    ("TOP PRIORITY (link building)", "top_priority"),
    ("Link building keywords", "linkbuilding_keywords"),
    ("Platform flag", "platform_flag"),
    ("Platform phrases", "platform_phrases"),
    ("Platform context", "platform_context"),
    ("Freelancer score (0-100)", "freelancer_score"),
    ("Freelancer label", "freelancer_label"),
    ("Freelancer reasons", "freelancer_reasons"),
    ("Keyword evidence URLs", "keyword_evidence_urls"),
    ("Impressum URL", "impressum_url"),
    ("Google categories", "categories"),
    ("Google rating", "rating"),
    ("Google reviews", "reviews"),
    ("Google Maps URL", "maps_url"),
    ("Maps listings (same domain)", "maps_listings"),
    ("Found by search", "found_by_keywords"),
    ("Checked by", "checked_by"),
    ("Status", "fetch_status"),
]


# --------------------------------------------------------------------------
# Selenium
# --------------------------------------------------------------------------
def new_driver(headless=True, page_timeout=30):
    from selenium import webdriver
    o = webdriver.ChromeOptions()
    if headless:
        o.add_argument("--headless=new")
    for a in ("--window-size=1366,900", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
              "--lang=de-DE", "--disable-notifications", "--mute-audio",
              "--blink-settings=imagesEnabled=false",
              "--disable-blink-features=AutomationControlled"):
        o.add_argument(a)
    o.add_experimental_option("excludeSwitches", ["enable-automation"])
    o.add_experimental_option("prefs", {"intl.accept_languages": "de-DE,de", "profile.managed_default_content_settings.images": 2})
    o.page_load_strategy = "eager"
    d = webdriver.Chrome(options=o)
    d.set_page_load_timeout(page_timeout)
    d.set_script_timeout(15)
    try:  # skip fonts, media and trackers: they only delay readyState=complete
        d.execute_cdp_cmd("Network.enable", {})
        d.execute_cdp_cmd("Network.setBlockedURLs", {"urls": BLOCKED_URLS})
    except Exception:  # noqa: BLE001
        pass
    return d


BLOCKED_URLS = [
    "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot", "*.mp4", "*.webm", "*.mp3", "*.ogg", "*.avi", "*.mov",
    "*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.avif", "*.svg", "*.ico",
    "*google-analytics.com*", "*googletagmanager.com*", "*doubleclick.net*", "*googlesyndication.com*",
    "*connect.facebook.net*", "*hotjar.com*", "*clarity.ms*", "*linkedin.com/px*", "*snap.licdn.com*",
    "*youtube.com/embed*", "*player.vimeo.com*", "*maps.googleapis.com*", "*google.com/maps*",
    "*fonts.googleapis.com*", "*fonts.gstatic.com*", "*use.typekit.net*", "*tiktok.com*", "*bing.com/bat*",
]

_NOT_FOUND = re.compile(r"\b404\b|nicht gefunden|not found|seite existiert nicht|page doesn.t exist", re.I)


def make_fetch(driver, settle=1.0, sub_settle=0.3):
    """The homepage gets the full render wait + scroll; follow-up pages (services, Impressum,
    contact) are mostly static and get a short wait."""
    from selenium.common.exceptions import TimeoutException
    calls = [0]

    def fetch(url):
        first = calls[0] == 0
        calls[0] += 1
        try:
            driver.get(url)
        except TimeoutException:
            pass  # use whatever has rendered so far
        # wait for document + JS rendering
        end = time.time() + (6 if first else 3)
        while time.time() < end:
            try:
                if driver.execute_script("return document.readyState") == "complete":
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        time.sleep(settle if first else sub_settle)
        # Cloudflare / bot challenge: give it a few seconds to clear
        for _ in range(6):
            t = (driver.title or "").lower()
            if "just a moment" in t or "moment bitte" in t or "attention required" in t:
                time.sleep(2)
            else:
                break
        if first:
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.5)
            except Exception:  # noqa: BLE001
                pass
        html = driver.page_source or ""
        title = driver.title or ""
        h1 = ""
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
        if m:
            h1 = re.sub(r"<[^>]+>", " ", m.group(1))[:200]
        status = 404 if (_NOT_FOUND.search(title) or _NOT_FOUND.search(h1)) else 200
        if C.looks_blocked(0, html) and len(C.page_text(C.parse_html(html))) < 400:
            status = 403
        return status, driver.current_url, html
    return fetch


def close_driver(driver):
    """quit() can fail on a hung or crashed Chrome and leave the browser running, so also kill
    chromedriver's whole process tree."""
    if driver is None:
        return
    try:
        root = psutil.Process(driver.service.process.pid)
        procs = [root] + root.children(recursive=True)
    except Exception:  # noqa: BLE001
        procs = []
    try:
        driver.quit()
    except Exception:  # noqa: BLE001
        pass
    for p in procs:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


def browser_worker(q, results, lock, bar, args, app):
    driver, used = None, 0
    while True:
        try:
            row = q.get_nowait()
        except queue.Empty:
            break
        try:
            if driver is None or used >= args.restart_every:
                close_driver(driver)
                driver, used = new_driver(not args.show, args.page_timeout), 0
            used += 1
            res = C.crawl_site(row.get("final_url") or row["website"], make_fetch(driver),
                               name=row["name"], max_service_pages=args.max_service_pages)
            if res["fetch_status"] == "error":   # maybe the redirect target was bad - try the Maps URL
                res2 = C.crawl_site(row["website"], make_fetch(driver), name=row["name"],
                                    max_service_pages=args.max_service_pages)
                if res2["fetch_status"] == "ok":
                    res = res2
        except Exception as e:  # noqa: BLE001  (driver crashed etc.)
            res = {k: "" for k in SITE_FIELDS}
            res.update({"fetch_status": "error", "fetch_error": f"{type(e).__name__}: {str(e)[:150]}"})
            close_driver(driver)
            driver = None
        res["domain"] = row["domain"]
        with lock:
            app.write(res)
            results[row["domain"]] = res
            bar.update(1)
        q.task_done()
    close_driver(driver)


# --------------------------------------------------------------------------
# Merge + export
# --------------------------------------------------------------------------
MAPS_FIELDS = ["name", "website", "country_code", "country", "city", "postcode", "address", "street", "phone",
               "phone_international", "categories", "primary_category", "rating", "reviews", "maps_url",
               "place_id", "cid", "claimed", "address_hidden", "ch_language", "latitude", "longitude",
               "found_by_keywords", "found_in_cities", "hit_count", "maps_listings", "other_listing_names"]


def refresh_maps_fields(step2_rows):
    """Step 2 may have run while step 1 was still collecting: refresh Maps data from the final step 1 CSV."""
    from step2_website_check import group_by_domain
    step1 = C.read_csv(C.STEP1_CSV)
    if not step1:
        return
    latest = {a["domain"]: a for a in group_by_domain(step1) if a.get("domain")}
    for r in step2_rows:
        a = latest.get(r.get("domain") or "")
        if a:
            for k in MAPS_FIELDS:
                if a.get(k) not in (None, ""):
                    r[k] = a[k]


def browser_worthy(r):
    """Can Chrome plausibly do better than curl_cffi here? Based on earlier browser runs:
    timeouts, blocked and near-empty (JS-rendered) pages often load; DNS/SSL errors and
    404/410 are dead sites, and pages with plenty of text but no keyword stay keyword-free."""
    fs = r.get("fetch_status") or ""
    if fs == "ok":
        return int(r.get("homepage_text_len") or 0) < 1500
    if fs == "error":
        return (r.get("fetch_error") or "").split(":")[0] not in ("DNSError", "SSLError")
    return fs not in ("http_404", "http_410")


def browser_yield_rank(r):
    """Queue order by keyword hit rate seen in earlier browser runs (lower = first):
    timeouts ~17%, 5xx/HTTP errors ~10-30%, near-empty pages ~8%, blocked ~4%,
    pages with 300-1500 chars of text <1%."""
    fs = r.get("fetch_status") or ""
    if fs == "ok":
        return 2 if int(r.get("homepage_text_len") or 0) < 300 else 5
    if fs == "error":
        return 0 if (r.get("fetch_error") or "").startswith(("Timeout", "HTTPError")) else 4
    if fs.startswith("http_5"):
        return 1
    return 3 if fs == "blocked" else 4


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
def merge(step2_rows, browser):
    final = []
    for r in step2_rows:
        out = dict(r)
        b = browser.get(r.get("domain") or "")
        if b:
            out["checked_by"] = "curl_cffi + selenium"
            b_ok = b.get("fetch_status") == "ok"
            r_ok = r.get("fetch_status") == "ok"
            if b_ok and (b.get("keywords_found") or not r_ok):
                # browser saw more (or step 2 failed): take the browser analysis,
                # but keep contact data from step 2 when the browser found none
                keep = {k: r.get(k, "") for k in ("general_email", "other_emails", "site_phone",
                                                   "managing_director", "md_label", "impressum_url")}
                for k in SITE_FIELDS:
                    out[k] = b.get(k, "")
                for k, v in keep.items():
                    if not out.get(k) and v:
                        out[k] = v
            else:
                # fill gaps only
                for k in ("general_email", "other_emails", "managing_director", "md_label", "impressum_url",
                          "site_phone"):
                    if not out.get(k) and b.get(k):
                        out[k] = b[k]
                if not r_ok and b.get("fetch_status"):
                    out["fetch_status"] = b["fetch_status"]
                    out["fetch_error"] = b.get("fetch_error", "")
        if not out.get("freelancer_score") and out.get("freelancer_score") != 0:
            s, lab, why = C.freelancer_score(out.get("name", ""))
            out["freelancer_score"], out["freelancer_label"], out["freelancer_reasons"] = s, lab, why
        out["phone_final"] = out.get("phone_international") or out.get("phone") or (out.get("site_phone") or "").split(",")[0]
        out["qualified"] = "yes" if out.get("keywords_found") else ""
        out["not_qualified_reason"] = "" if out["qualified"] else (
            "no website" if out.get("fetch_status") == "no_website" else
            "only social/platform profile" if out.get("fetch_status") == "social_only" else
            f"website not reachable ({out.get('fetch_status')})" if out.get("fetch_status") != "ok" else
            "no qualifying keyword on homepage/service pages")
        final.append(out)
    return final


def export_xlsx(final, path):
    import pandas as pd
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    kw_cols = [f"KW: {k}" for k in C.QUALIFY_KEYWORDS]

    def frame(rows, extra=(), keyword_columns=False):
        cols = FINAL_COLUMNS + list(extra)
        data = []
        for r in rows:
            d = {h: r.get(f, "") for h, f in cols}
            if keyword_columns:
                found = {k.strip() for k in (r.get("keywords_found") or "").split(",")}
                for k in C.QUALIFY_KEYWORDS:
                    d[f"KW: {k}"] = "✓" if k in found else ""
            data.append(d)
        names = [h for h, _ in cols] + (kw_cols if keyword_columns else [])
        return pd.DataFrame(data, columns=names)

    qual = [r for r in final if r["qualified"]]
    qual.sort(key=lambda r: (r.get("platform_flag") == "YES", r.get("top_priority") != "YES",
                             -int(r.get("keyword_count") or 0), r.get("country", ""), r.get("city", "")))
    notq = [r for r in final if not r["qualified"]]
    notq.sort(key=lambda r: (r["not_qualified_reason"], r.get("country", ""), r.get("city", "")))

    info = [
        ("Generated", time.strftime("%Y-%m-%d %H:%M")),
        ("Agencies (qualified)", len(qual)),
        ("  of which TOP PRIORITY (link building terms)", sum(1 for r in qual if r.get("top_priority") == "YES")),
        ("  of which platform flag", sum(1 for r in qual if r.get("platform_flag") == "YES")),
        ("  of which likely freelancer (score >= 65)", sum(1 for r in qual if r.get("freelancer_label") == "likely freelancer")),
        ("  with general email", sum(1 for r in qual if r.get("general_email"))),
        ("  with managing director", sum(1 for r in qual if r.get("managing_director"))),
        ("Not qualified", len(notq)),
        ("", ""),
        ("Qualifying keywords", ", ".join(C.QUALIFY_KEYWORDS)),
        ("Top priority keywords", ", ".join(k for k in C.QUALIFY_KEYWORDS if k in C.LINKBUILDING_KEYWORDS)),
        ("Platform phrases (flag)", ", ".join(C.PLATFORM_PHRASES)),
        ("Keyword scope", "Homepage + up to 4 service pages (Leistungen/SEO/Linkbuilding links). "
                          "Separators are flexible: 'Link-Building' counts as Linkbuilding."),
        ("Platform scope", "All checked pages (homepage, service, about, Impressum)."),
        ("Freelancer score", "0-100, higher = more likely a freelancer. Name signals (personal name, 'Freelancer', "
                             "'Berater'), site signals (ich vs. wir, 'über mich', 'Freelancer'), Impressum "
                             "(GmbH/HRB lowers, Kleinunternehmer raises). One-person agencies presenting as an "
                             "agency score low. Labels: >=65 likely freelancer, 45-64 unclear, <45 agency."),
        ("MD source", "Geschäftsführer / Vertreten durch / Inhaber / Medieninhaber = labelled in Impressum. "
                      "'Name im Impressum (Adressblock)' and 'Verantwortlich für Inhalt' are best guesses."),
        ("Dedup", "One row per website domain (Maps listings with the same domain merged)."),
    ]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        frame(qual, keyword_columns=True).to_excel(xw, sheet_name="Agencies", index=False)
        frame(notq, [("Reason", "not_qualified_reason")]).to_excel(xw, sheet_name="Not qualified", index=False)
        pd.DataFrame(info, columns=["Item", "Value"]).to_excel(xw, sheet_name="Info", index=False)

        widths = {"Company name": 38, "Website": 32, "Address": 34, "Other emails": 30, "Keywords found": 45,
                  "Platform context": 50, "Freelancer reasons": 50, "Keyword evidence URLs": 45,
                  "Google categories": 30, "Managing director": 28, "General email": 28, "Reason": 34}
        top_fill = PatternFill("solid", fgColor="E2F0D9")
        flag_fill = PatternFill("solid", fgColor="FCE4D6")
        for name in ("Agencies", "Not qualified"):
            ws = xw.sheets[name]
            ws.freeze_panes = "B2"
            ws.auto_filter.ref = ws.dimensions
            headers = [c.value for c in ws[1]]
            for i, h in enumerate(headers, 1):
                ws.column_dimensions[get_column_letter(i)].width = widths.get(h, 16)
                ws.cell(1, i).font = Font(bold=True)
                ws.cell(1, i).alignment = Alignment(wrap_text=True, vertical="top")
            if name == "Agencies":
                ti = headers.index("TOP PRIORITY (link building)") + 1
                pi = headers.index("Platform flag") + 1
                for row in ws.iter_rows(min_row=2):
                    if row[pi - 1].value == "YES":
                        for c in row:
                            c.fill = flag_fill
                    elif row[ti - 1].value == "YES":
                        for c in row:
                            c.fill = top_fill
                # one column per keyword: ✓ cells highlighted (dark green = link building term)
                lb_fill = PatternFill("solid", fgColor="70AD47")
                kw_fill = PatternFill("solid", fgColor="BDD7EE")
                for i, h in enumerate(headers, 1):
                    if not str(h).startswith("KW: "):
                        continue
                    ws.column_dimensions[get_column_letter(i)].width = 11
                    ws.cell(1, i).alignment = Alignment(wrap_text=True, vertical="top", text_rotation=90)
                    is_lb = h[4:] in C.LINKBUILDING_KEYWORDS
                    for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                        if row[0].value == "✓":
                            row[0].fill = lb_fill if is_lb else kw_fill
                            row[0].alignment = Alignment(horizontal="center")
        wi = xw.sheets["Info"]
        wi.column_dimensions["A"].width = 44
        wi.column_dimensions["B"].width = 120
        for row in wi.iter_rows(min_row=2):
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    return len(qual), len(notq)


def main():
    C.utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=C.STEP2_CSV)
    ap.add_argument("--output", default=C.STEP3_CSV)
    ap.add_argument("--xlsx", default=C.FINAL_XLSX)
    ap.add_argument("--workers", type=int, default=3, help="parallel Chrome instances")
    ap.add_argument("--max-service-pages", type=int, default=3)
    ap.add_argument("--page-timeout", type=int, default=30)
    ap.add_argument("--restart-every", type=int, default=40, help="restart Chrome after N sites")
    ap.add_argument("--limit", type=int, default=0, help="only re-check N sites (test)")
    ap.add_argument("--show", action="store_true", help="visible browser")
    ap.add_argument("--js-only", action="store_true",
                    help="only re-check sites a browser can plausibly recover (blocked, timeout, 5xx, near-empty "
                         "page); skip dead domains (DNS/SSL errors, 404/410) and sites that loaded fine "
                         "but simply have no keyword")
    ap.add_argument("--skip-low-yield", action="store_true",
                    help="also skip pages that loaded with 300-1500 chars of text (<1%% keyword hits in a browser)")
    ap.add_argument("--max-rank", type=int, default=None,
                    help="only sites with browser_yield_rank <= N (0 timeouts, 1 5xx, 2 near-empty, 3 blocked, "
                         "4 other errors, 5 text but no keyword)")
    ap.add_argument("--countries", default=None, help="only these country codes, e.g. DE,CH")
    ap.add_argument("--export-only", action="store_true", help="skip Selenium, just merge + export")
    args = ap.parse_args()

    C.ensure_dirs()
    step2 = C.read_step2(args.input)
    if not step2:
        raise SystemExit(f"No rows in {args.input} - run step 2 first.")

    browser = {r["domain"]: r for r in C.read_csv(BROWSER_CSV)}
    todo = [r for r in step2 if C.truthy(r.get("needs_browser")) and r.get("domain") and r.get("checked_by")
            and r.get("website_type", "website") == "website" and r["domain"] not in browser]
    if args.js_only:
        todo = [r for r in todo if browser_worthy(r)]
    if args.skip_low_yield:
        todo = [r for r in todo if browser_yield_rank(r) < 5]
    if args.max_rank is not None:
        todo = [r for r in todo if browser_yield_rank(r) <= args.max_rank]
    todo.sort(key=browser_yield_rank)
    if args.countries:
        wanted = {c.strip().upper() for c in args.countries.split(",")}
        todo = [r for r in todo if r.get("country_code") in wanted]
    if args.limit:
        todo = todo[: args.limit]
    print(f"Step 2 agencies: {len(step2)} | browser-checked before: {len(browser)} | to check now: "
          f"{0 if args.export_only else len(todo)}")

    if todo and not args.export_only:
        q = queue.Queue()
        for r in todo:
            q.put(r)
        lock = threading.Lock()
        app = C.CsvAppender(BROWSER_CSV, BROWSER_FIELDS)
        bar = tqdm(total=len(todo), desc="selenium", unit="site")
        threads = [threading.Thread(target=browser_worker, args=(q, browser, lock, bar, args, app), daemon=True)
                   for _ in range(max(1, min(args.workers, len(todo))))]
        try:
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nInterrupted - progress saved, re-run to resume (Chrome windows close on exit).")
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            for t in threads:
                t.join(timeout=60)
        finally:
            bar.close()
            app.close()
        recovered = sum(1 for r in todo if browser.get(r["domain"], {}).get("keywords_found"))
        print(f"Selenium: {recovered}/{len(todo)} re-checked sites now have keyword matches")

    refresh_maps_fields(step2)
    final = merge(step2, browser)
    fields = list(dict.fromkeys([f for _, f in FINAL_COLUMNS] + list(step2[0].keys()) +
                                ["phone_final", "qualified", "not_qualified_reason"]))
    C.write_csv(args.output, final, fields)
    try:
        nq, nn = export_xlsx(final, args.xlsx)
    except PermissionError:
        alt = args.xlsx.replace(".xlsx", f"_{int(time.time())}.xlsx")
        print(f"{args.xlsx} is open in Excel - writing {alt} instead")
        nq, nn = export_xlsx(final, alt)
    print(f"\nFinal: {nq} qualified agencies, {nn} not qualified\n-> {args.output}\n-> {args.xlsx}")


if __name__ == "__main__":
    main()
