# SEO Agencies DACH Scraper

Built by [Talha Pythoneer](https://www.talhapythoneer.com), web scraping and AI agents.

Finds SEO and link building agencies in Germany, Austria and German-speaking Switzerland.
It searches Google Maps, checks each agency's website for SEO and link building keywords,
and exports one row per domain to CSV and Excel.

## Pipeline

| Step | Script | Input | Output |
|---|---|---|---|
| 1 | `step1_google_maps.py` | GeoNames city list (auto-downloaded) | `output/step1_maps.csv` |
| 2 | `step2_website_check.py` | `output/step1_maps.csv` | `output/step2_websites.csv` |
| 3 | `step3_selenium_check.py` | `output/step2_websites.csv` | `output/step3_final.csv`, `output/seo_agencies_final.xlsx` |

`common.py` holds everything the steps share: keyword lists, platform phrases, the Impressum,
email and phone extractors, freelancer scoring and the site crawler. Steps 2 and 3 run the
same analysis; step 2 fetches pages with curl_cffi and step 3 renders them in Chrome.

Each step is a standalone script. Each one resumes where it stopped, so you can interrupt
it with Ctrl+C and run it again.

### Step 1: Google Maps
- City list: GeoNames `cities1000`, meaning every place with at least 1,000 inhabitants, plus city
  districts (Berlin-Kreuzberg, Hamburg-Altona and so on). That is about 10,080 places. Searches use the
  German place names (München, Wien, Nürnberg). For Switzerland, only German-speaking cantons are
  searched, with known French and Italian towns removed. The list is cached in
  `data/cities_*.csv`, which you can edit.
- Searches go through Google Maps' internal JSON search endpoint (no browser), up to 120 results
  (6 pages) each. Paging stops early once a page only returns places already found.
- 20 search keywords in German and English (`MAPS_SEARCH_KEYWORDS`). 6 core keywords (SEO Agentur,
  Linkbuilding Agentur, Online Marketing Agentur, Suchmaschinenoptimierung, SEO agency, Webagentur)
  are searched in every town. 14 more (SEO Beratung, SEO Experte, Linkaufbau, Backlinks, Offpage SEO,
  Local SEO Agentur, Digitalagentur, Internetagentur, Werbeagentur, digital marketing agency,
  link building agency, SEO consultant, search engine optimization, SEO services) are searched in
  towns of 5,000+, since Maps already covers the surrounding area there. That is about 110k searches.
- Fields kept: name, categories, website, phone, address, postcode, city, country, rating,
  reviews, coordinates, opening hours, place id/cid, Maps URL, claimed status, which searches
  found it, and a freelancer score based on the name.
- Out of scope and removed: listings outside DE/AT/CH and Swiss listings with French or Italian
  postcodes. These go to `output/step1_excluded.csv`.
- Raw results go to `output/step1_raw.jsonl`. Use `--export-only` to rebuild the CSV from it.

### Step 2: Website check (curl_cffi)
- Deduplicates by domain; the listing with the most reviews is kept.
- Fetches the homepage, up to 4 service pages (links mentioning Leistungen/SEO/Linkbuilding),
  the about page, the Impressum and, if no general email was found, the contact page.
- **Keywords** are matched on the homepage and service pages only, as the brief requires.
  Spacing and hyphens are flexible, so "Link-Building" counts as Linkbuilding.
  `keyword_evidence_urls` lists the pages where they were found. English equivalents are also
  matched (SEO agency, search engine optimization, SEO consulting, on-page/off-page optimization,
  keyword research), marked "(EN)".
- **Top priority**: the site contains Linkbuilding, Linkaufbau, Backlinks, Backlinkaufbau,
  Backlink-Aufbau, Offpage, Off-Page-Optimierung, Offpage-SEO or Linkprofil.
- **Platform flag**: the site contains "Backlinks kaufen", "Marktplatz für Backlinks" or
  "Publisher-Netzwerk" on any checked page. `platform_context` shows the surrounding text.
- **Managing director**: taken from the Impressum. Labelled names (Geschäftsführer, Vertreten durch,
  Inhaber, Medieninhaber) are used first. Fallbacks are an inline form ("X, Geschäftsführer"), the
  owner's name in the address block, and "Verantwortlich für den Inhalt". `md_label` shows which
  source was used.
- **Email**: `general_email` is a generic inbox such as info@, kontakt@, office@ or hallo@, preferring
  the agency's own domain. All other addresses go in `other_emails`. Cloudflare-protected and
  `[at]`-obfuscated addresses are decoded.
- Sites that failed, were blocked or had no keyword match get `needs_browser=yes`.

### Step 3: Selenium re-check and export
- Re-checks every site where step 2 found no keyword (plus failed/blocked ones), since some content
  only appears after JavaScript runs. It re-checks the `needs_browser` sites in headless Chrome, with the page fully rendered, using the
  same analysis as step 2. Results are merged back into the step 2 data.
- `run_pipeline.sh` runs step 3 with `--js-only --max-rank 3`: only timeouts, 5xx errors, near-empty
  (JavaScript-rendered) pages and blocked pages, in that order (highest hit rate first). Dead domains
  (DNS/SSL errors, 404/410), other odd errors and pages that loaded fine without a keyword are skipped,
  because Chrome found keywords on under 1% of them in test runs.
- `seo_agencies_final.xlsx` has three sheets:
  - **Agencies**: qualifying agencies, sorted with top priority first. Top-priority rows are green
    and platform-flagged rows are orange. At the end there is one `KW: <keyword>` column per
    keyword with a ✓ where it was found (dark green = link building term).
  - **Not qualified**: everything else, with the reason (no keyword, no website, unreachable).
  - **Info**: counts, keyword definitions and scoring notes.

### Freelancer score (0-100)
A higher score means more likely a freelancer. One-person agencies that present themselves as an
agency score low.

| Signal | Points |
|---|---|
| Name contains "Freelancer" or "Freiberufler" | +45 |
| Name looks like a person ("Max Muster - SEO") | +25 |
| Name contains Berater, Experte or Consultant | +15 |
| Site says Freelancer | +25 |
| Site uses "ich" far more than "wir" | +20 |
| Site has an "Über mich" page | +15 |
| Impressum says Kleinunternehmer | +10 |
| Name or Impressum shows a legal form (GmbH, UG, HRB, Firmenbuch) | −30 to −40 |
| Name contains "Agentur" | −20 |
| Site calls itself an Agentur, or mentions a team | −10 to −15 |

Labels: 65 or more is "likely freelancer", 45-64 is "unclear", below 45 is "agency".
`freelancer_reasons` shows the points behind each score.

## Setup

```bash
pip install -r requirements.txt
```
Step 3 needs Google Chrome. Selenium Manager downloads the matching driver automatically.

## Usage

```bash
# test batch
python step1_google_maps.py --cities "München,Wien,Zürich,Hamburg" --keywords "SEO Agentur,Linkbuilding Agentur"
python step2_website_check.py --limit 100
python step3_selenium_check.py

# full run, one step after the other
python step1_google_maps.py --workers 16
for k in 0 1 2 3; do python step2_website_check.py --shard $k/4 --workers 16 & done; wait
python step3_selenium_check.py --js-only --max-rank 3 --workers 8

# or unattended: steps 2 and 3 run on rolling batches while step 1 is still collecting
bash run_pipeline.sh        # logs: output/pipeline.log, output/step*_run.log
```

Useful flags:
- step 1: `--countries DE`, `--min-pop 5000`, `--max-cities 50`, `--no-districts`,
  `--proxies proxies.txt` (one proxy URL per line), `--delay 1.5`, `--export-only`
- step 2: `--limit N`, `--workers N`, `--countries`, `--shard k/N`, `--retry-failed` (forgets errored
  rows and exits; run step 2 again to re-check them)
- step 3: `--limit N`, `--workers N`, `--js-only` (only sites a browser can recover), `--max-rank N` (only queue
  groups up to N: 0 timeouts, 1 5xx, 2 near-empty, 3 blocked, 4 other errors, 5 text but no keyword), `--countries`,
  `--show` (visible browser), `--export-only`

Keep step 2 at about 64 threads in total (4 shards x 16). More than that floods the local DNS
resolver and live sites get recorded as "could not resolve host". Lookups that fail are retried twice.

If Google starts blocking step 1 (10 failures in a row), all workers pause for 15 minutes and then
continue. Failed searches are retried when the same command is run again.

## Output

[`sample_output/sample_agencies.csv`](sample_output/sample_agencies.csv) shows the full column layout of
the Agencies sheet with a few made-up rows (placeholder names, `example.com` domains, dummy phone
numbers). Real output is written to `output/`, which is gitignored.

Main columns, with example values:

| Column | Example |
|---|---|
| Company name | Example SEO GmbH |
| Website | https://www.muster-seo.example.com/ |
| Country / City / Postcode | Germany / München / 80331 |
| General email | info@muster-seo.example.com |
| Phone | +49 89 1234567 |
| Managing director | Max Mustermann |
| Keywords found | Linkbuilding, Offpage, SEO Agentur |
| TOP PRIORITY (link building) | YES |
| Platform flag | (empty) |
| Freelancer score / label | 0 / agency |

## Config

Edit the top of `common.py` to change:
- `MAPS_SEARCH_KEYWORDS`: the Google Maps searches
- `QUALIFY_KEYWORDS`: the website keywords (name to regex)
- `LINKBUILDING_KEYWORDS`: which keywords count as top priority
- `PLATFORM_PHRASES`: the link-platform flags
