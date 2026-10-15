"""
Shared config + helpers for the SEO agency pipeline (DE / AT / CH-German).

step1_google_maps.py  -> output/step1_maps.csv
step2_website_check.py -> output/step2_websites.csv
step3_selenium_check.py -> output/step3_final.csv + output/seo_agencies_final.xlsx

Everything the client may want to tweak (keywords, platform phrases, scoring)
lives at the top of this file.
"""
import csv
import html as html_lib
import os
import re
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlparse, unquote, parse_qs

from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
OUTPUT_DIR = os.path.join(ROOT, "output")

STEP1_CSV = os.path.join(OUTPUT_DIR, "step1_maps.csv")
STEP2_CSV = os.path.join(OUTPUT_DIR, "step2_websites.csv")
STEP3_CSV = os.path.join(OUTPUT_DIR, "step3_final.csv")
FINAL_XLSX = os.path.join(OUTPUT_DIR, "seo_agencies_final.xlsx")

COUNTRIES = {"DE": "Germany", "AT": "Austria", "CH": "Switzerland"}

# --------------------------------------------------------------------------
# Google Maps search terms (step 1). Each is combined with every city.
# --------------------------------------------------------------------------
#   (keyword, minimum town population). 0 = searched in every town (>= 1k inhabitants),
#   5000 = only in towns >= 5k (Maps already shows the surrounding area there).
MAPS_SEARCH_KEYWORDS = [
    # core - every town
    ("SEO Agentur", 0),
    ("Linkbuilding Agentur", 0),
    ("Online Marketing Agentur", 0),
    ("Suchmaschinenoptimierung", 0),
    ("SEO agency", 0),
    ("Webagentur", 0),
    # extended - towns >= 5k
    ("SEO Beratung", 5000),
    ("SEO Experte", 5000),
    ("Linkaufbau", 5000),
    ("Backlinks", 5000),
    ("Offpage SEO", 5000),
    ("Local SEO Agentur", 5000),
    ("Digitalagentur", 5000),
    ("Internetagentur", 5000),
    ("Werbeagentur", 5000),
    ("digital marketing agency", 5000),
    ("link building agency", 5000),
    ("SEO consultant", 5000),
    ("search engine optimization", 5000),
    ("SEO services", 5000),
]

# --------------------------------------------------------------------------
# Qualifying keywords (client brief). Name shown in the sheet -> regex.
# Separators are flexible ("Link-Building", "Link Building", "Linkbuilding").
# --------------------------------------------------------------------------
_S = r"[\s\-‐‑]?"  # optional space / hyphen
QUALIFY_KEYWORDS = {
    "Linkbuilding": rf"\blink{_S}building",
    "Linkaufbau": rf"\blink{_S}aufbau",
    "Backlinks": rf"\bback{_S}links?\b",
    "Backlinkaufbau": r"\bbacklinkaufbau",
    "Backlink-Aufbau": r"\bbacklink[\s\-‐‑]aufbau",
    "Offpage": rf"\boff{_S}page\b",
    "Off-Page-Optimierung": rf"\boff{_S}page{_S}optimierung",
    "Offpage-SEO": rf"\boff{_S}page{_S}seo\b",
    "Linkprofil": rf"\blink{_S}profil",
    "Suchmaschinenoptimierung": rf"\bsuchmaschinen{_S}optimierung",
    "SEO-Agentur": r"\bseo[\-‐‑]agentur",
    "SEO Agentur": r"\bseo\s+agentur",
    "SEO-Beratung": rf"\bseo{_S}beratung",
    "SEO-Betreuung": rf"\bseo{_S}betreuung",
    "SEO-Optimierung": rf"\bseo{_S}optimierung",
    "Local SEO": rf"\b(?:local|lokale[sn]?|lokal){_S}seo\b",
    "Onpage-Optimierung": rf"\bon{_S}page{_S}optimierung",
    "Keyword-Recherche": rf"\bkeyword{_S}recherche",
    # English equivalents (DACH agencies with English sites)
    "Off-page optimization (EN)": rf"\boff{_S}page{_S}optimi[sz]ation",
    "SEO agency (EN)": r"\bseo\s+agency",
    "Search engine optimization (EN)": rf"\bsearch{_S}engine{_S}optimi[sz]ation",
    "SEO consulting (EN)": rf"\bseo{_S}(?:consulting|consultancy|consultant)",
    "On-page optimization (EN)": rf"\bon{_S}page{_S}optimi[sz]ation",
    "Keyword research (EN)": rf"\bkeyword{_S}research",
}
# Explicit link building terms -> "top priority"
LINKBUILDING_KEYWORDS = {
    "Linkbuilding", "Linkaufbau", "Backlinks", "Backlinkaufbau", "Backlink-Aufbau",
    "Offpage", "Off-Page-Optimierung", "Offpage-SEO", "Linkprofil",
    "Off-page optimization (EN)",
}
# Phrases that suggest a link marketplace / platform rather than an agency
PLATFORM_PHRASES = {
    "Backlinks kaufen": r"\bbacklinks?\s+kaufen",
    "Marktplatz für Backlinks": r"\bmarktplatz\s+f(?:ü|ue)r\s+backlinks",
    "Publisher-Netzwerk": rf"\bpublisher{_S}netzwerk",
}

_QUALIFY_RE = {k: re.compile(v, re.I) for k, v in QUALIFY_KEYWORDS.items()}
_PLATFORM_RE = {k: re.compile(v, re.I) for k, v in PLATFORM_PHRASES.items()}

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "xing.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.", "fiverr.com", "upwork.com", "freelancermap.",
    "malt.de", "malt.com", "goo.gl", "g.page", "google.com", "wa.me", "t.me",
)


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def clean_maps_url(url):
    """Maps sometimes wraps websites in /url?q=... redirects."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("/url?") or "google.com/url?" in url:
        q = parse_qs(urlparse(url).query).get("q")
        if q:
            url = q[0]
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    return url


def get_domain(url):
    """Host without www., lowercase. Used as dedupe key."""
    if not url:
        return ""
    try:
        host = urlparse(clean_maps_url(url)).hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def is_social_or_platform(domain):
    return any(domain == h or domain.endswith("." + h) or (h.endswith(".") and h in domain)
               for h in SOCIAL_HOSTS)


def norm_text(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = s.replace("\xad", "").replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fieldnames=None):
    if fieldnames is None:
        fieldnames = []
        for r in rows:
            for k in r:
                if k not in fieldnames:
                    fieldnames.append(k)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    for attempt in range(30):   # Windows: target may be open by another step for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(1)


class CsvAppender:
    """Append rows to a CSV (writes header once). Used for resumable steps."""

    def __init__(self, path, fieldnames):
        self.path = path
        self.fieldnames = fieldnames
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.f = open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="")
        self.w = csv.DictWriter(self.f, fieldnames=fieldnames, extrasaction="ignore")
        if new:
            self.w.writeheader()
            self.f.flush()

    def write(self, row):
        self.w.writerow(row)
        self.f.flush()

    def close(self):
        self.f.close()


def step2_part_path(k):
    return os.path.join(OUTPUT_DIR, f"step2_websites.part{k}.csv")


def read_step2(path=None):
    """Step 2 results: main file plus shard part files (step2_websites.partN.csv)."""
    import glob
    path = path or STEP2_CSV
    rows = read_csv(path)
    if os.path.abspath(path) == os.path.abspath(STEP2_CSV):
        for f in sorted(glob.glob(os.path.join(OUTPUT_DIR, "step2_websites.part*.csv"))):
            rows += read_csv(f)
    return rows


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y")


# --------------------------------------------------------------------------
# Switzerland: keep German-speaking areas only (postcode based)
# --------------------------------------------------------------------------
def ch_language_region(postcode):
    """Rough language region from a Swiss postcode: de / fr / it / bilingual / ''."""
    try:
        pc = int(str(postcode).strip()[:4])
    except (ValueError, TypeError):
        return ""
    if 1700 <= pc <= 1799:
        return "bilingual"            # Fribourg / Sense district
    if 1000 <= pc <= 2499 or 2600 <= pc <= 2999:
        return "fr"                   # Romandie, Neuchâtel, Jura, Bernese Jura
    if 2500 <= pc <= 2599:
        return "bilingual"            # Biel/Bienne + Seeland
    if 3960 <= pc <= 3968 or 3971 <= pc <= 3979:
        return "fr"                   # Sierre / Crans-Montana area
    if 6500 <= pc <= 6999:
        return "it"                   # Ticino + Misox
    if 7600 <= pc <= 7610 or 7741 <= pc <= 7748:
        return "it"                   # Bregaglia, Poschiavo
    return "de"


# French / Italian speaking Swiss towns in otherwise mixed cantons (city list)
CH_NON_GERMAN_TOWNS = {
    "Saint-Imier", "Tramelan", "Moutier", "La Neuveville", "Villars-sur-Glâne", "Romont",
    "Marly", "Estavayer-le-Lac", "Châtel-Saint-Denis", "Bulle", "Sitten", "Sion", "Sierre",
    "Savièse", "Monthey", "Martigny-Ville", "Martigny", "Fully", "Conthey", "Collombey",
    "Basse-Nendaz", "Bagnes", "Crans-Montana", "Vétroz", "Saxon", "Chalais", "Grône",
    "Roveredo", "Poschiavo",
}
CH_GERMAN_CANTONS = {
    "AG", "AI", "AR", "BE", "BL", "BS", "FR", "GL", "GR", "LU", "NW", "OW", "SG", "SH",
    "SO", "SZ", "TG", "UR", "VS", "ZG", "ZH",
}

# --------------------------------------------------------------------------
# Freelancer scoring
# --------------------------------------------------------------------------
LEGAL_FORM_RE = re.compile(   # capital companies / partnerships
    r"(?<![\w])(gmbh|ug\b|ug \(haftungsbeschr|ag\b|kg\b|ohg\b|gbr\b|mbh|"
    r"ltd\.?|inc\.?|s\.?a\.?r\.?l|sàrl|se\b|og\b|partg|llc|co\. kg|holding|gesmbh)",
    re.I,
)
SOLE_TRADER_RE = re.compile(r"(?<![\w])(e\.\s?[ku]\.?(?!\w)|e\.\s?kfm|e\.\s?kfr|einzelunternehm)", re.I)
FREELANCE_WORD_RE = re.compile(r"\b(freelancer(?:in)?|freelance|freiberufler(?:in)?|freiberuflich|"
                               r"selbst(?:st)?ändige[rn]?\s+(?:seo|online|marketing))", re.I)
CONSULTANT_NAME_RE = re.compile(r"\b(seo[\s\-]?berater(?:in)?|berater(?:in)?|consultant|"
                                r"seo[\s\-]?expert(?:e|in)?|expert(?:e|in)|coach|trainer(?:in)?|"
                                r"seo[\s\-]?manager(?:in)?|spezialist(?:in)?)\b", re.I)
AGENCY_NAME_RE = re.compile(r"(agentur|agency|agence|studio|group|gruppe|team|media|digital|"
                            r"solutions|consulting|partner|werb|marketing|kommunikation|design|"
                            r"works|factory|netz|web|online|seo|software|"
                            r"services?|systems?|concept|konzept|creative|kreativ|brand|pixel)", re.I)

FIRST_NAMES = set("""
aaron adam adrian alex alexander alexandra ali alina amelie andre andrea andreas angela angelika anja anke anna
anne annika anton antonia arne axel barbara bastian ben benedikt benjamin bernd bernhard bettina birgit bjoern björn
boris carina carla carmen carolin caroline carsten celine chantal charlotte chiara christian christiane christina
christine christoph christopher claudia clemens cornelia daniel daniela david denis dennis diana dieter dirk dominik
doris eduard elena elias elisabeth emil emily emma erik erika eva fabian falk felix florian frank franz franziska
frederik friedrich gabriele georg gerald gerhard gregor guido gunnar günter guenter hannah hannes hans harald heiko
heinz helena helmut hendrik henning henrik herbert holger hubert ida ines ingo ingrid isabel isabell jakob jan jana
janina janine jannik jasmin jens jessica joachim jochen johann johanna johannes jonas jonathan josef julia julian
juliane julius jörg joerg jürgen juergen justus kai karin karl karsten katharina kathrin katja katrin kerstin kevin
kilian kim klaus konrad kristina lara lars laura lea lena leon leonie lisa lorenz louis luca lucas luis luisa lukas
lutz manfred manuel manuela marc marcel marco marcus mareike maria marie marina mario marius mark markus marlene
martin martina mathias matthias max maximilian melanie michael michaela miriam moritz nadine nadja natalie niklas
nico nicolas nicole niels nils nina noah norbert olaf oliver pascal patrick patricia paul paula peter petra philipp
rainer ralf ralph raphael rebecca reinhard rene renate richard robert robin roland roman ronny rudolf ruth sabine
sabrina sandra sara sarah sascha sebastian selina simon simone sonja sophia sophie stefan stefanie steffen stephan
stephanie susanne sven svenja sylvia tanja theresa thomas thorsten tim timo tobias tom torsten ulrich ulrike ursula
uwe valentin vanessa vera verena viktor viktoria walter werner wolfgang yannick yvonne
""".split())


def _personal_name_like(name):
    """'Jörg Zimmer - SEO Freelancer' -> True. 'Max Media GmbH' -> False."""
    head = re.split(r"\s[\-–|:,]\s|\s\||[|,(]", name or "")[0].strip()
    tokens = head.split()
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    first = tokens[0].lower().strip(".")
    if first in ("dr", "prof", "dipl.-ing", "mag", "ing"):
        tokens = tokens[1:]
        first = tokens[0].lower() if tokens else ""
    if first not in FIRST_NAMES or len(tokens) < 2:
        return False
    second = tokens[1]
    if not second[:1].isupper() or AGENCY_NAME_RE.search(second) or "seo" in second.lower():
        return False
    return True


def _count(pattern, text):
    return len(re.findall(pattern, text or "", re.I))


def freelancer_score(name, home_text="", about_text="", impressum_text="", nav_text=""):
    """
    0..100, higher = more likely a freelancer (not an agency).
    One-person agencies presenting themselves as an agency score low.
    Returns (score, label, reasons).
    """
    pts = 40
    reasons = []

    def add(p, why):
        nonlocal pts
        pts += p
        reasons.append(f"{why}({'+' if p > 0 else ''}{p})")

    name = name or ""
    if LEGAL_FORM_RE.search(name):
        add(-40, "name:legal-form")
    elif SOLE_TRADER_RE.search(name):
        add(-10, "name:registered-sole-trader")
    if re.search(r"agentur|agency|agence", name, re.I):
        add(-20, "name:agentur")
    if FREELANCE_WORD_RE.search(name):
        add(45, "name:freelancer-word")
    elif CONSULTANT_NAME_RE.search(name) and not re.search(r"agentur|agency", name, re.I):
        add(15, "name:consultant/expert")
    if _personal_name_like(name):
        add(25, "name:personal-name")

    site = " ".join(x for x in (home_text, about_text) if x)
    if site:
        if FREELANCE_WORD_RE.search(site):
            add(25, "site:freelancer-word")
        i_cnt = _count(r"\b(ich|mich|mir|mein|meine|meinen|meinem|meiner)\b", site)
        we_cnt = _count(r"\b(wir|uns|unser|unsere|unseren|unserem|unserer)\b", site)
        total = i_cnt + we_cnt
        if total >= 8:
            ratio = i_cnt / total
            if ratio >= 0.6:
                add(20, f"site:first-person-singular({i_cnt}ich/{we_cnt}wir)")
            elif ratio <= 0.25:
                add(-15, f"site:first-person-plural({i_cnt}ich/{we_cnt}wir)")
        if re.search(r"\b(unsere|eine|ihre|die)\s+(?:[\w\-]+\s+){0,3}agentur\b", site, re.I):
            add(-15, "site:calls-itself-agentur")
        if re.search(r"\bunser(?:em)?\s+team\b|\bunsere\s+mitarbeiter", site, re.I):
            add(-10, "site:team")
    if nav_text:
        if re.search(r"über\s+mich|ueber[\s\-]mich|about[\s\-]me", nav_text, re.I):
            add(15, "nav:über-mich")
        elif re.search(r"über\s+uns|ueber[\s\-]uns|about[\s\-]us|\bteam\b", nav_text, re.I):
            add(-10, "nav:über-uns/team")
    if impressum_text:
        if LEGAL_FORM_RE.search(impressum_text) or re.search(r"\bHRB\s*\d", impressum_text):
            add(-30, "impressum:company-register")
        elif SOLE_TRADER_RE.search(impressum_text) or re.search(r"\bHRA\s*\d|handelsregister|firmenbuch",
                                                                impressum_text, re.I):
            add(-10, "impressum:registered-sole-trader")
        if re.search(r"kleinunternehmer|§\s*19\s*ustg", impressum_text, re.I):
            add(10, "impressum:kleinunternehmer")
        if re.search(r"freiberufler|freiberuflich|freelancer", impressum_text, re.I):
            add(15, "impressum:freelancer-word")

    score = max(0, min(100, pts))
    label = "likely freelancer" if score >= 65 else ("unclear" if score >= 45 else "agency")
    return score, label, "; ".join(reasons)


# --------------------------------------------------------------------------
# HTML analysis
# --------------------------------------------------------------------------
def parse_html(html):
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def page_text(soup):
    """Visible text + title + meta description, normalised."""
    parts = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string)
    for m in soup.find_all("meta", attrs={"name": re.compile(r"^description$", re.I)}):
        if m.get("content"):
            parts.append(m["content"])
    body = soup.body or soup
    for t in body.find_all(["script", "style", "noscript", "svg", "template", "iframe"]):
        t.decompose()
    parts.append(body.get_text(" "))
    return norm_text(" ".join(parts))


def page_lines(soup):
    """Visible text split into lines (for Impressum parsing)."""
    body = soup.body or soup
    for t in body.find_all(["script", "style", "noscript", "svg", "template", "iframe"]):
        t.decompose()
    for br in body.find_all("br"):
        br.replace_with("\n")
    raw = body.get_text("\n")
    lines = [norm_text(l) for l in raw.split("\n")]
    return [l for l in lines if l]


def find_keywords(text):
    return [k for k, rx in _QUALIFY_RE.items() if rx.search(text or "")]


def find_platform_phrases(text):
    return [k for k, rx in _PLATFORM_RE.items() if rx.search(text or "")]


def platform_context(text, width=70):
    """Short snippets around platform phrases so the client can judge them quickly."""
    out = []
    for k, rx in _PLATFORM_RE.items():
        m = rx.search(text or "")
        if m:
            a, b = max(0, m.start() - width), min(len(text), m.end() + width)
            out.append("…" + text[a:b].strip() + "…")
    return out


# ---------- emails ----------
# Bounded repeats (RFC length limits) keep this linear on long base64/whitespace blobs; unbounded
# versions take minutes on such pages and hold the GIL, freezing every thread in the process.
EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.[A-Za-z]{2,24}")
GENERIC_PREFIXES = [
    "info", "kontakt", "office", "contact", "hallo", "hello", "mail", "anfrage", "anfragen", "service",
    "team", "post", "buero", "büro", "agentur", "welcome", "moin", "servus", "hi", "seo", "marketing",
    "sales", "vertrieb", "support", "kundenservice", "zentrale", "email", "e-mail", "projekte", "projekt",
]
_BAD_EMAIL_DOMAINS = ("example.", "domain.", "sentry", "wixpress.com", "yourdomain", "ihredomain",
                      "mustermann", "email.com", "email.at", "email.de", "test.", "beispiel.", "firma.",
                      "trustindex.io", "provenexpert", "hotmail.com", "jimdo.com", "wix.com", "godaddy.com",
                      "w3.org", "schema.org", "sentry.io", "cloudflare.com", "google.com", "elementor.com",
                      "wordpress.org", "wordpress.com", "ionos.", "strato.de", "lightwidget", "mysite.",
                      "deinedomain", "meinedomain", "website.", "company.", "unternehmen.")
_BAD_EMAIL_LOCAL = ("name", "vorname", "your", "ihre", "deine", "max.mustermann", "email", "user", "noreply",
                    "no-reply", "donotreply", "beispiel", "abuse", "postmaster", "hostmaster", "webmaster",
                    "privacy", "datenschutz-anfrage", "john.doe", "jane.doe", "vorname.nachname",
                    "your.name", "ihr.name", "dein.name", "muster")
# Common TLDs: used to cut off text glued to an address ("info@x.comOffice" -> "info@x.com")
_KNOWN_TLDS = sorted(
    "com de at ch net org eu info io biz co agency digital media online berlin wien swiss marketing design "
    "studio solutions consulting gmbh team email me app dev ai li tirol koeln hamburg bayern nrw ruhr saarland "
    "cologne ag business expert services tech gl tv fm cc pro shop store website site space world global "
    "partners group company works rocks seo click link xyz one uk us nl fr it es pl hu cz sk si hr be lu dk se no".split(),
    key=len, reverse=True)


def _fix_tld(e):
    local, _, dom = e.partition("@")
    head, _, tld = dom.rpartition(".")
    if not head or tld in _KNOWN_TLDS:
        return e
    for t in _KNOWN_TLDS:
        if tld.startswith(t):
            return f"{local}@{head}.{t}"
    return e


def _decode_cfemail(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def extract_emails(soup, html):
    found = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            found.append(unquote(href[7:].split("?")[0]))
        m = re.search(r"/cdn-cgi/l/email-protection#([0-9a-f]+)", href, re.I)
        if m:
            found.append(_decode_cfemail(m.group(1)))
    for el in soup.find_all(attrs={"data-cfemail": True}):
        found.append(_decode_cfemail(el["data-cfemail"]))
    text = html_lib.unescape(html or "").replace("%20", " ").replace("%40", "@")
    text = re.sub(r"\\u0040|\\x40", "@", text)
    text = re.sub(r"\s{0,3}[\[\(\{]\s{0,3}(?:at|ät|@)\s{0,3}[\]\)\}]\s{0,3}", "@", text, flags=re.I)
    text = re.sub(r"\s{0,3}[\[\(\{]\s{0,3}(?:dot|punkt|\.)\s{0,3}[\]\)\}]\s{0,3}", ".", text, flags=re.I)
    found += EMAIL_RE.findall(text)
    out = []
    for e in found:
        e = _fix_tld(e.strip().strip(".").lower())
        if not EMAIL_RE.fullmatch(e or ""):
            continue
        local, _, dom = e.partition("@")
        if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js)$", e) or re.search(r"@\d+x\.", e):
            continue
        if any(b in dom for b in _BAD_EMAIL_DOMAINS) or local in _BAD_EMAIL_LOCAL:
            continue
        if re.fullmatch(r"[0-9a-f]{16,}", local):
            continue
        if e not in out:
            out.append(e)
    return out


def pick_emails(emails, domain):
    """-> (general_email, other_emails[list]) preferring same-domain generic inboxes."""
    base = (domain or "").split(".")[-2] if domain and "." in domain else (domain or "")

    def same_dom(e):
        d = e.split("@")[1]
        return bool(domain) and (d == domain or d.endswith("." + domain) or domain.endswith("." + d)
                                 or (base and base in d))

    def generic_rank(e):
        local = e.split("@")[0]
        for i, p in enumerate(GENERIC_PREFIXES):
            if local == p or local.startswith(p + ".") or local.startswith(p + "-"):
                return i
        return None

    generic = [e for e in emails if generic_rank(e) is not None]
    generic.sort(key=lambda e: (not same_dom(e), generic_rank(e)))
    general = generic[0] if generic else ""
    others = [e for e in emails if e != general]
    others.sort(key=lambda e: not same_dom(e))
    return general, others[:5]


# ---------- phones ----------
def extract_phones(soup):
    out = []
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("tel:"):
            p = re.sub(r"[^\d+]", "", unquote(a["href"][4:]))
            if 6 <= len(p.lstrip("+")) <= 15 and p not in out:
                out.append(p)
    return out


# ---------- managing director ----------
_GF = r"gesch(?:ä|ae)ftsf(?:ü|ue)hr(?:er(?:in|innen)?|ung|ende[rn]?(?:\s+gesellschafter(?:in)?)?)"
_MD_LABELS = [
    ("Geschäftsführer", re.compile(
        rf"^\W*(?:vertretungsberechtigt?e[rn]?\s+|alleinige[rn]?\s+)?(?:{_GF}|gesch(?:ä|ae)ftsleitung|"
        rf"managing\s+directors?|ceo)\b\s*(?:\(.*?\))?\s*[:\-–]?\s*(.*)$", re.I)),
    ("Vertreten durch", re.compile(
        r"^\W*(?:vertreten\s+durch|vertretungsberechtigt?(?:e[rn]?)?(?:\s+person(?:en)?)?|zeichnungsberechtigt(?:e[rn]?)?(?:\s+person(?:en)?)?|"
        r"represented\s+by)\b\s*[:\-–]?\s*(.*)$", re.I)),
    ("Inhaber", re.compile(
        r"^\W*(?:firmen)?(?:inhaber(?:in)?|unternehmer(?:in)?|owner|betreiber(?:in)?|"
        r"einzelunternehmer(?:in)?|gesellschafter(?:in)?)\b\s*[:\-–]?\s*(.*)$", re.I)),
    ("Medieninhaber", re.compile(r"^\W*medieninhaber(?:in)?\b\s*[:\-–,]?\s*(.*)$", re.I)),
]
_MD_RESPONSIBLE = re.compile(
    r"^\W*(?:f(?:ü|ue)r\s+den\s+inhalt\s+verantwortlich|verantwortlich\s+f(?:ü|ue)r\s+(?:den\s+)?inhalt|"
    r"inhaltlich\s+verantwortlich|verantwortlich(?:e[rn]?)?\s+(?:i\.?\s?s\.?\s?d\.?|im\s+sinne|gem(?:ä|ae)(?:ß|ss)|nach)|"
    r"v\.?\s?i\.?\s?s\.?\s?d\.?\s?p\.?)[^:]{0,60}[:\-–]?\s*(.*)$", re.I)
_MD_FILLER = re.compile(
    r"^(?:inh\.|den|die|der|des\s+unternehmens|der\s+firma|durch|und|herausgeber(?:in)?|diensteanbieter(?:in)?|"
    rf"{_GF}|gesellschafter(?:in)?|vorstand|inhaber(?:in)?|herrn?|frau|ceo|the|managing\s+director|"
    r"ist|sind|alleinige[rn]?|vertretungsberechtigte[rn]?)\b\s*[:\-–,]?\s*", re.I)
_MD_STOP = re.compile(
    r"\b(registergericht|handelsregister|amtsgericht|registernummer|register|hrb|hra|ust|umsatzsteuer|"
    r"steuer|sitz|telefon|tel\b|e-mail|email|fax|kontakt|anschrift|firmenbuch|uid|verantwortlich|"
    r"inhaltlich|aufsichtsbeh|mitglied|kammer|adresse|www\.|http)", re.I)
_TITLE_TOKEN = re.compile(
    r"^(?:dr|prof|dipl|mag|ing|di|dkfm|mmag|rer|soc|oec|nat|med|phil|jur|pol|techn|habil|fh|univ|ao|"
    r"b\.?a|m\.?a|mba|msc|bsc|ll\.?m|ll\.?b|phd|beng|meng|eur|dipl\.?-[\w\.]+|dr\.?-[\w\.]+|"
    r"mas|mim|cmc|ba|ma|bed|mmsc|dipl\.?[\w\.\-]*)\.?$", re.I)
_PARTICLES = {"von", "van", "der", "den", "de", "zu", "vom", "di", "da", "del", "la", "le", "ten", "ter", "zur", "dos"}
_NOT_NAME = re.compile(
    r"(?:agentur|agency|marketing|design|media|digital|consult|solutions|studio|werbe|verwaltung|holding|"
    r"gesellschaft|gesch(?:ä|ae)ftsbedingungen|kanzlei|software|systeme)|"
    r"\b(?:gmbh|gesmbh|ug\b|ag\b|kg\b|og\b|keg\b|oeg\b|gbr\b|ohg\b|ltd\b|inc\b|llc\b|sarl\b|sàrl\b|"
    r"standort|filiale|niederlassung|b(?:ü|ue)ro\b|office\b|zentrale|sitz\b|agb\b|allgemeine|"
    r"verwaltung|holding|beteiligung|services?\b|gruppe\b|group\b|team\b|seo\b|online\b|web\b|design\b|"
    r"netz\b|impressum|angaben|gem(?:ä|ae)(?:ß|ss)|tmg\b|ecg\b|mediengesetz|datenschutz|kontakt|anschrift|"
    r"gesch(?:ä|ae)ftsf|vertreten|deutschland|(?:ö|oe)sterreich|schweiz|eingetragen|einzelunternehm|unternehmen|"
    r"linkedin|xing\b|profil\b|firma\b|gesellschaft|register|gewerbe|berufs|haftung|urheber|links?\b|"
    r"inhalt|hinweis|rechte\b|cookie|e\.\s?u\.|e\.\s?k\.)|"
    r"(?:stra(?:ß|ss)e|str\.|gasse|weg|platz|allee)\b", re.I)


def _valid_name(p):
    """'Mag. (FH) Peter Bono' -> same string if it looks like a person name, else ''."""
    p = re.sub(r"\s+", " ", p or "").strip(" .,:;-–")
    p = re.sub(r"^inh(?:aber(?:in)?)?\.?:?\s+", "", p, flags=re.I)
    if not p or len(p) > 70 or _NOT_NAME.search(p) or re.search(r"[\d@]", p):
        return ""
    core = [t for t in re.split(r"[\s,]+", p) if t and not _TITLE_TOKEN.match(t.strip("().,"))]
    words = 0
    for t in core:
        t = t.strip("()")
        if t.lower() in _PARTICLES:
            continue
        if not (t[:1].isupper() and re.fullmatch(r"[^\W\d_]+(?:[\-'’][^\W\d_]+)*\.?", t)):
            return ""
        words += 1
    return p if 2 <= words <= 4 else ""


def _clean_names(chunk):
    chunk = _MD_STOP.split(chunk or "")[0]
    chunk = re.sub(r"\((?!fh\))[^)]{3,}\)", " ", chunk, flags=re.I)   # "(Co-Gründer xyz)" but keep "(FH)"
    names = []
    parts = re.split(r",(?!\s*(?:mba|msc|bsc|ma|ba|b\.a|m\.a|ll\.?m|phd|mas|beng|meng)\b)|;|\s+und\s+|\s+and\s+|&|/|\||•|·",
                     chunk, flags=re.I)
    for part in parts:
        p = part.strip(" .:-–\t")
        while True:
            q = _MD_FILLER.sub("", p).strip(" .:-–,")
            if q == p:
                break
            p = q
        v = _valid_name(p)
        if v:
            names.append(v)
    return names


_INLINE_MD = [
    # "Max Mustermann (Geschäftsführer)" / "Max Mustermann, Geschäftsführer" / "Max Mustermann – CEO"
    re.compile(rf"([^\W\d_][^,()\n]{{3,60}}?)\s*(?:\(|,|–|-|\|)\s*(?:gründer(?:in)?\s*(?:&|und)\s*)?(?:{_GF}|ceo|inhaber(?:in)?)\b", re.I),
    # "Dr. Max Mustermann vertritt selbstständig als Geschäftsführer"
    re.compile(rf"^([^\W\d_][^,()\n]{{3,60}}?)\s+(?:ist|vertritt)\b.{{0,60}}?{_GF}", re.I),
]
_STREETISH = re.compile(r"(stra(?:ß|ss)e|str\.|gasse|weg|platz|allee|ring|damm|ufer|markt|hof|berg)\b.*\d|"
                        r"^\D{3,40}\s\d{1,4}\s?[a-z]?(?:[/\-]\d+)*$", re.I)
_SOLE_PROP = re.compile(r"\be\.\s?[uk]\.|einzelunternehm|e\.\s?kfm|e\.\s?kfr", re.I)


def _is_company_name(candidate, company):
    """True if every word of the candidate also appears in the business name (e.g. 'Studio Hills')."""
    words = {w.lower() for w in re.findall(r"[^\W\d_]{2,}", candidate or "") if not _TITLE_TOKEN.match(w)}
    comp = {w.lower() for w in re.findall(r"[^\W\d_]{2,}", company or "")}
    return bool(words) and words <= comp


def extract_managing_director(lines, company=""):
    """Impressum lines -> (names joined with '; ', label) or ('', '').
    `company` (business name) is used to reject the firm name in the heuristic fallbacks."""
    # 1) labelled lines ("Geschäftsführer: ...", "Inhaber: ...")
    for label, rx in _MD_LABELS:
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            rest = (m.group(1) or "").strip()
            names = _clean_names(rest) if rest else []
            if not names:
                # value on the next line(s); several directors may follow one per line
                for j in range(i + 1, min(len(lines), i + 7)):
                    if any(r.match(lines[j]) for _, r in _MD_LABELS):
                        break
                    found = _clean_names(lines[j]) if len(lines[j]) <= 80 else []
                    if found:
                        names += found
                    elif names or j >= i + 2:
                        break
            if names:
                return "; ".join(list(dict.fromkeys(names))[:4]), label
    # 2) inline "Name, Geschäftsführer" forms
    for line in lines:
        for rx in _INLINE_MD:
            m = rx.search(line)
            if m:
                names = _clean_names(m.group(1))
                if names:
                    return names[-1], "Geschäftsführer"
    # 3) address block of a sole proprietorship / small firm: name line followed by a street line
    start = next((i for i, l in enumerate(lines) if re.fullmatch(r"impressum|imprint", l, re.I)), 0)
    for i in range(start, min(len(lines), start + 40)):
        line = lines[i]
        m = re.match(r"^(.*?)\s*,?\s*e\.\s?[uk]\.?\s*$", line, re.I)   # "Mag. Gerwin Gfrerer e.U."
        if m and _valid_name(m.group(1)) and not _is_company_name(m.group(1), company):
            return _valid_name(m.group(1)), "Inhaber (Firmenname)"
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        prv = lines[i - 1] if i > 0 else ""
        if len(line) <= 60 and (_STREETISH.search(nxt) or _SOLE_PROP.search(prv)):
            v = _valid_name(line)
            if v and not _is_company_name(v, company):
                return v, "Name im Impressum (Adressblock)"
    # 4) person responsible for content (often the owner in small agencies)
    for i, line in enumerate(lines):
        m = _MD_RESPONSIBLE.match(line)
        if m:
            names = _clean_names(m.group(1) or "") or (_clean_names(lines[i + 1]) if i + 1 < len(lines) else [])
            if names:
                return "; ".join(names[:2]), "Verantwortlich für Inhalt"
    return "", ""


# ---------- link discovery ----------
_IMPRESSUM_RE = re.compile(r"impressum|imprint|legal[\-_ ]?notice|anbieterkennzeichnung|rechtliche[\-_ ]hinweise|offenlegung", re.I)
_KONTAKT_RE = re.compile(r"kontakt|contact", re.I)
_ABOUT_RE = re.compile(r"(?:ueber|über|uber)[\-_ ]?(?:uns|mich)|about|team|wer[\-_ ]wir[\-_ ]sind", re.I)
_SERVICE_HINTS = [
    (re.compile(r"link[\-_ ]?building|link[\-_ ]?aufbau|backlink|off[\-_ ]?page|linkprofil", re.I), 5),
    (re.compile(r"\bseo\b|seo[\-_]|suchmaschinen", re.I), 3),
    (re.compile(r"leistung|services?|angebot|l(?:ö|oe)sungen|kompetenz|was[\-_ ]wir|portfolio|marketing", re.I), 1),
]
_SKIP_PATH = re.compile(
    r"/(?:blog|news|magazin|magazine|wiki|glossar|lexikon|tag|tags|category|kategorie|author|autor|"
    r"jobs?|karriere|career|wp-content|wp-json|feed|referenzen|cases?)/|"
    r"\.(?:pdf|jpe?g|png|gif|svg|webp|zip|mp4|docx?|xlsx?)(?:$|\?)", re.I)


def classify_links(soup, base_url):
    host = get_domain(base_url)
    impressum, kontakt, about, services = [], [], [], {}
    nav_bits = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        if get_domain(url) != host:
            continue
        label = norm_text(a.get_text(" "))[:80]
        both = f"{urlparse(url).path} {label}"
        nav_bits.append(label)
        if _IMPRESSUM_RE.search(both):
            if url not in impressum:
                impressum.append(url)
            continue
        if _KONTAKT_RE.search(both):
            if url not in kontakt:
                kontakt.append(url)
            continue
        if _ABOUT_RE.search(both) and url not in about:
            about.append(url)
        if _SKIP_PATH.search(url) or urlparse(url).path in ("", "/"):
            continue
        score = sum(w for rx, w in _SERVICE_HINTS if rx.search(both))
        if score:
            services[url] = max(score, services.get(url, 0))
    ranked = sorted(services, key=lambda u: (-services[u], len(u)))
    impressum.sort(key=lambda u: (0 if "impressum" in u.lower() else 1, len(u)))
    return {"impressum": impressum, "kontakt": kontakt, "about": about,
            "services": ranked, "nav_text": " | ".join(nav_bits)}


IMPRESSUM_FALLBACK_PATHS = ["/impressum", "/impressum/", "/impressum.html", "/imprint", "/de/impressum"]


# --------------------------------------------------------------------------
# Site crawl shared by step 2 (curl_cffi) and step 3 (Selenium).
# `fetch(url) -> (status:int, final_url:str, html:str)`; raise on network error.
# --------------------------------------------------------------------------
def looks_blocked(status, html):
    h = (html or "")[:20000].lower()
    return status in (401, 403, 429, 503) or any(s in h for s in (
        "just a moment...", "cf-browser-verification", "challenge-platform", "attention required! | cloudflare",
        "ddos-guard", "please enable javascript", "enable javascript to run this app",
        "sgcaptcha", "captcha-delivery"))


def crawl_site(start_url, fetch, name="", max_service_pages=4):
    res = {
        "final_url": "", "http_status": "", "fetch_status": "", "fetch_error": "",
        "pages_checked": 0, "keywords_found": "", "keyword_count": 0, "top_priority": "",
        "linkbuilding_keywords": "", "keyword_evidence_urls": "", "platform_flag": "",
        "platform_phrases": "", "platform_context": "", "general_email": "", "other_emails": "", "site_phone": "",
        "managing_director": "", "md_label": "", "impressum_url": "", "homepage_text_len": 0,
        "freelancer_score": "", "freelancer_label": "", "freelancer_reasons": "",
    }
    try:
        status, final_url, html = fetch(start_url)
    except Exception as e:  # noqa: BLE001
        res["fetch_status"] = "error"
        res["fetch_error"] = f"{type(e).__name__}: {str(e)[:150]}"
        return res
    res["http_status"] = status
    res["final_url"] = final_url
    if not html or status >= 400:
        res["fetch_status"] = "blocked" if looks_blocked(status, html) else f"http_{status}"
        return res

    emails, phones = [], []

    def digest(h, with_links=False):
        """Parse a page once: links (optional), emails, phones, then visible text."""
        sp = parse_html(h)
        lk = classify_links(sp, final_url) if with_links else None
        for e in extract_emails(sp, h):
            if e not in emails:
                emails.append(e)
        for ph in extract_phones(sp):
            if ph not in phones:
                phones.append(ph)
        return page_text(sp), lk

    home_text, links = digest(html, with_links=True)
    res["homepage_text_len"] = len(home_text)
    all_text = [home_text]
    kw_hits = {}  # keyword -> first url
    for k in find_keywords(home_text):
        kw_hits.setdefault(k, final_url)
    pages = 1

    # services pages (keywords only count on homepage + service pages)
    for url in links["services"][:max_service_pages]:
        try:
            st, fu, h = fetch(url)
        except Exception:  # noqa: BLE001
            continue
        pages += 1
        if st >= 400 or not h:
            continue
        t, _ = digest(h)
        all_text.append(t)
        for k in find_keywords(t):
            kw_hits.setdefault(k, fu)

    # about page (freelancer signals only)
    about_text = ""
    for url in links["about"][:1]:
        try:
            st, fu, h = fetch(url)
            pages += 1
            if st < 400 and h:
                about_text, _ = digest(h)
                all_text.append(about_text)
        except Exception:  # noqa: BLE001
            pass

    # impressum
    imp_lines, imp_html, imp_url = [], "", ""
    candidates = links["impressum"][:2] or [urljoin(final_url, p) for p in IMPRESSUM_FALLBACK_PATHS[:3]]
    for url in candidates:
        try:
            st, fu, h = fetch(url)
        except Exception:  # noqa: BLE001
            continue
        pages += 1
        if st < 400 and h and re.search(r"impressum|imprint|angaben gem|offenlegung|legal", h, re.I):
            imp_html, imp_url = h, fu
            imp_lines = page_lines(parse_html(h))
            digest(h)
            break

    general, others = pick_emails(emails, get_domain(final_url))

    # contact page only if we still have no general email
    if not general and links["kontakt"]:
        try:
            st, fu, h = fetch(links["kontakt"][0])
            pages += 1
            if st < 400 and h:
                digest(h)
                general, others = pick_emails(emails, get_domain(final_url))
        except Exception:  # noqa: BLE001
            pass

    md, md_label = extract_managing_director(imp_lines, name) if imp_lines else ("", "")
    platform, platform_ctx = [], []
    for t in all_text + ([" ".join(imp_lines)] if imp_lines else []):
        for p in find_platform_phrases(t):
            if p not in platform:
                platform.append(p)
        for c in platform_context(t):
            if len(platform_ctx) < 3 and c not in platform_ctx:
                platform_ctx.append(c)

    kws = [k for k in QUALIFY_KEYWORDS if k in kw_hits]
    lb = [k for k in kws if k in LINKBUILDING_KEYWORDS]
    score, label, reasons = freelancer_score(
        name, home_text=home_text, about_text=about_text,
        impressum_text=" ".join(imp_lines), nav_text=links["nav_text"])

    res.update({
        "fetch_status": "ok",
        "pages_checked": pages,
        "keywords_found": ", ".join(kws),
        "keyword_count": len(kws),
        "top_priority": "YES" if lb else "",
        "linkbuilding_keywords": ", ".join(lb),
        "keyword_evidence_urls": " | ".join(dict.fromkeys(kw_hits[k] for k in kws)),
        "platform_flag": "YES" if platform else "",
        "platform_phrases": ", ".join(platform),
        "platform_context": " | ".join(platform_ctx),
        "general_email": general,
        "other_emails": ", ".join(others),
        "site_phone": ", ".join(phones[:3]),
        "managing_director": md,
        "md_label": md_label,
        "impressum_url": imp_url,
        "freelancer_score": score,
        "freelancer_label": label,
        "freelancer_reasons": reasons,
    })
    if not kws and looks_blocked(status, html):
        res["fetch_status"] = "blocked"
    return res
