"""
rfx_alert.py
------------
Traffic / civil engineering RFx monitor. Pulls open solicitations from:

  RELIABLE (API-based, run anywhere):
    - SAM.gov            federal opportunities API
    - NYC CROL           City Record Online (Socrata Open Data API)
    - NJDOT              consultant advertisement scrape (best-effort)

  JS PORTALS (bot-detected, handled in rfx_playwright_sources.py):
    - PANYNJ Bonfire, NYC PASSPort, Suffolk County, Nassau County

Filters everything through the shared keyword list, then emails an HTML digest
via Gmail SMTP.

RUN:   py rfx_alert.py
"""

import os
import re
import sys
import json
import smtplib
import datetime as dt
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

from rfx_common import matches, result, NAICS_CODES

# ─── CONFIG ───────────────────────────────────────────────────────────────────
# Best practice: set these as environment variables. If you'd rather just paste
# them in, replace the "" fallback with your value (keep the quotes).
SAM_API_KEY       = os.environ.get("SAM_API_KEY", "")            # from sam.gov
GMAIL_ADDRESS     = os.environ.get("GMAIL_ADDRESS", "")          # you@gmail.com
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")    # 16-char app pw
RECIPIENT         = os.environ.get("RFX_RECIPIENT", "") or GMAIL_ADDRESS

LOOKBACK_DAYS = 7           # SAM.gov window each run
# SAM.gov is filtered to these place-of-performance states (GPI is regional, and
# NAICS 541330 federal engineering is otherwise a national firehose). Widen or
# clear this set to see more.
SAM_STATES = {"NY", "NJ"}
CROL_LOOKBACK_DAYS = 21     # CROL publishes in batches; a wider window catches more.
                           # Bump to ~120 for a one-time catch-up, then drop back.
INCLUDE_PLAYWRIGHT = True  # set False to skip the four JS portals

# The digest always shows every currently-open opportunity, with the ones that are
# NEW since the last run highlighted and sorted to the top of each source. A small
# JSON file remembers what's already been seen so "new" can be computed. The very
# first run has no history, so nothing is flagged new that time; every run after
# flags genuinely new postings.
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rfx_seen.json")

# NJDOT Bureau of Professional Services — current consultant solicitations.
# This page is entirely transportation consulting, so all rows are listed
# (no keyword filter). Set to None to disable.
NJDOT_URL = "https://www.nj.gov/transportation/business/procurement/ProfServ/CurrentSolic.shtm"
# Show only solicitations still open for proposals ("Advertised"), not ones
# already closed and in "Pending Selection". Set False to see everything.
NJDOT_ADVERTISED_ONLY = True

# NYSDOT engineering consultant advertisements (server-rendered; all rows are
# transportation work, so no keyword filter). Set None to disable.
NYSDOT_URL = "https://www.dot.ny.gov/portal/page/portal/doing-business/opportunities/eng-detailad"

# NYS Contract Reporter — this is your pre-filtered search URL (category 5 +
# your counties + Status=Open). Results are listed as-is, no extra keyword
# filter, since you've already filtered server-side. Set None to disable.
# NYS Contract Reporter — filtered to your full NYSDOT Region 8/10/11 county
# footprint (15 counties), Status=Open, Category 5; keyword filter narrows further.
NYSCR_URL = "https://www.nyscr.ny.gov/Ads/Search?Skip=0&UseBookmarks=&UseNotifications=&UseProfile=&SubcontractId=&DivisionId=&Status=Open&Keyword=&DateFilter=All&GovernmentId=&Categories%5B%5D=5&Counties%5B%5D=31&Counties%5B%5D=53&Counties%5B%5D=4&Counties%5B%5D=25&Counties%5B%5D=32&Counties%5B%5D=42&Counties%5B%5D=44&Counties%5B%5D=14&Counties%5B%5D=15&Counties%5B%5D=37&Counties%5B%5D=41&Counties%5B%5D=45&Counties%5B%5D=54&Counties%5B%5D=57&Counties%5B%5D=61&Top=50&Sort=Category"

# Nassau County formal solicitation board (Oracle APEX, but server-rendered so
# it scrapes statically). Keeps Open + Amended, drops Closed. Set None to disable.
NASSAU_URL = "https://apex5.nassaucountyny.gov/ords/f?p=533:226"

# MTA Construction & Development — current opportunities (static Drupal page; this
# is where the real engineering/A-E work lives, vs the empty agency hubs). Each
# biddable item has a "Solicitation Notice" PDF link. Set None to disable.
MTA_URL = "https://www.mta.info/agency/construction-and-development/contracting/current-opportunities"
# NOTE: this page 403s from cloud/datacenter IPs (Akamai edge-level IP-
# reputation block — confirmed via GitHub Actions log; not fixable with
# headers or browser rendering, since Akamai denies before the request
# reaches MTA's server). The new2.mta.info mirror doesn't resolve (dead
# domain) so that's not a workaround either. Works fine from a home/
# residential IP — see README for the hybrid-run option.

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RFxMonitor/1.0"}
# NOTE: this is deliberately NOT spoofed to look like real Chrome. An earlier
# attempt to fix MTA's 403 by claiming a real Chrome UA (plus Accept/
# Accept-Language headers) may have made things worse — Python's requests
# library has a TLS/connection fingerprint that doesn't match real Chrome at
# all, so claiming to BE Chrome while not behaving like it is a classic bot-
# detection trigger. This honest, plainly-labeled UA is what MTA originally
# accepted, before any of that troubleshooting started.


def _mdy_dates(text: str) -> list:
    """Extract all M/D/Y dates from text as date objects (2-digit years -> 20xx)."""
    out = []
    for mm, dd, yy in re.findall(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text):
        y = int(yy)
        if y < 100:
            y += 2000
        try:
            out.append(dt.date(y, int(mm), int(dd)))
        except ValueError:
            pass
    return out


# ─── SAM.gov ──────────────────────────────────────────────────────────────────
def _pop_state(o: dict) -> str:
    """Pull the 2-letter state from a SAM opportunity, trying a few shapes."""
    pop = o.get("placeOfPerformance") or {}
    st = pop.get("state") if isinstance(pop, dict) else None
    if isinstance(st, dict):
        code = st.get("code") or st.get("name") or ""
    else:
        code = st or ""
    if not code:
        office = o.get("officeAddress") or {}
        code = office.get("state", "") if isinstance(office, dict) else ""
    return (code or "").strip().upper()[:2]


def fetch_samgov() -> list[dict]:
    if not SAM_API_KEY:
        print("[SAM.gov] No API key set — skipping.")
        return []

    base = "https://api.sam.gov/prod/opportunities/v2/search"
    posted_from = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    posted_to   = dt.date.today().strftime("%m/%d/%Y")
    out = []

    # Loop NAICS codes; SAM filters one ncode per request.
    for ncode in NAICS_CODES:
        offset = 0
        while True:
            params = {
                "api_key": SAM_API_KEY,
                "postedFrom": posted_from,
                "postedTo": posted_to,
                "ncode": ncode,
                "limit": 100,
                "offset": offset,
            }
            try:
                r = requests.get(base, params=params, headers=HEADERS, timeout=40)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"[SAM.gov] error (ncode {ncode}): {e}")
                break

            rows = data.get("opportunitiesData", []) or []
            for o in rows:
                # Geo filter: keep only solicitations performed in target states.
                # (NAICS already constrains to engineering server-side, so a
                # keyword re-check here is redundant and was the source of the
                # earlier "everything matches" bug.)
                state = _pop_state(o)
                if SAM_STATES and state not in SAM_STATES:
                    continue
                title = o.get("title", "")
                agency = o.get("fullParentPathName", "")
                deadline = (o.get("responseDeadLine") or "")[:10] or "Unknown"
                out.append(result(
                    "SAM.gov", agency, title,
                    o.get("uiLink", ""),
                    date=o.get("postedDate", ""), due=deadline,
                    extra=f"{state or '??'} | {o.get('solicitationNumber') or ''}",
                ))
            total = data.get("totalRecords", 0)
            offset += 100
            if offset >= total or not rows:
                break

    print(f"[SAM.gov] {len(out)} matches")
    return out


# ─── NYC City Record Online (Socrata) ─────────────────────────────────────────
def fetch_crol() -> list[dict]:
    base = "https://data.cityofnewyork.us/resource/dg92-zbpx.json"
    cutoff = (dt.date.today() - dt.timedelta(days=CROL_LOOKBACK_DAYS)).isoformat()
    # Field-agnostic full-text searches on the core terms, unioned + deduped.
    seed_terms = ["traffic", "roadway", "transportation", "intersection",
                  "pedestrian", "signal", "highway", "engineering"]
    seen, out = set(), []

    for term in seed_terms:
        try:
            r = requests.get(base, params={
                "$q": term,
                "$where": f"start_date >= '{cutoff}T00:00:00'",
                "$order": "start_date DESC",
                "$limit": 500,
            }, headers=HEADERS, timeout=40)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:
            print(f"[CROL] error (term {term}): {e}")
            continue

        for row in rows:
            rid = (row.get("request_id") or row.get("requestid")
                   or row.get(":id") or str(row))
            if rid in seen:
                continue
            title = (row.get("short_title") or row.get("shorttitle")
                     or row.get("title") or "")
            agency = (row.get("agency_name") or row.get("agencyname") or "")
            notice = (row.get("type_of_notice_description")
                      or row.get("typeofnoticedescription") or "")
            date = (row.get("start_date") or row.get("startdate") or "")[:10]
            due = ((row.get("due_date") or row.get("duedate")
                    or row.get("end_date") or row.get("enddate") or "")[:10]
                   or "Unknown")
            desc = (row.get("description") or row.get("additional_description_1") or "")

            # Only actual solicitations (drops awards, personnel notices, etc.).
            if "solicit" not in notice.lower():
                continue
            # Match on TITLE + DESCRIPTION only — never the agency name, or every
            # notice from "Department of Transportation" would qualify.
            if not matches(title, desc):
                continue

            seen.add(rid)
            cr_ref = row.get("request_id") or row.get("requestid") or ""
            # CROL notices live at /RequestDetail/{request_id}.
            link = (f"https://a856-cityrecord.nyc.gov/RequestDetail/{cr_ref}"
                    if cr_ref else "https://a856-cityrecord.nyc.gov/")
            out.append(result(
                "NYC CROL", agency, title or "(no title)",
                link, date=date, due=due,
                extra=f"{notice} | Ref {cr_ref}" if cr_ref else notice,
            ))

    print(f"[CROL] {len(out)} matches (last {CROL_LOOKBACK_DAYS}d)")
    return out


# ─── NJDOT (best-effort scrape) ───────────────────────────────────────────────
def fetch_njdot() -> list[dict]:
    if not NJDOT_URL:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[NJDOT] beautifulsoup4 not installed — skipping.")
        return []
    try:
        r = requests.get(NJDOT_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[NJDOT] error: {e}")
        return []

    import re
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    tp_re = re.compile(r"TP-?\d+", re.I)

    # Each solicitation row has a TP-#### link; the description lives in sibling
    # cells. Find the TP anchors, then read their whole table row.
    for a in soup.find_all("a"):
        label = a.get_text(strip=True)
        if not tp_re.search(label):
            continue
        tr = a.find_parent("tr")
        if tr is None:
            continue
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        rowtext = " | ".join(c for c in cells if c)
        if not rowtext or rowtext in seen:
            continue
        # Skip closed/in-evaluation rows unless the user wants them.
        if NJDOT_ADVERTISED_ONLY and "advertised" not in rowtext.lower():
            continue
        seen.add(rowtext)
        href = a.get("href", "")
        if href and not href.startswith("http"):
            href = requests.compat.urljoin(NJDOT_URL, href)
        njdates = _mdy_dates(rowtext)
        due = max(njdates).strftime("%m/%d/%Y") if njdates else "Unknown"
        # No keyword filter — this page is all transportation consulting.
        out.append(result("NJDOT", "NJDOT", rowtext, href or NJDOT_URL,
                          due=due, extra=label))

    print(f"[NJDOT] {len(out)} solicitations")
    return out


# ─── NYSDOT (engineering consultant advertisements) ───────────────────────────
def fetch_nysdot() -> list[dict]:
    if not NYSDOT_URL:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[NYSDOT] beautifulsoup4 not installed — skipping.")
        return []
    try:
        r = requests.get(NYSDOT_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[NYSDOT] error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    today = dt.date.today()
    text = soup.get_text(" ", strip=True)
    out, seen = [], set()

    # The page is one run-on text blob, not a clean table. Each ad looks like:
    #   "... PIN# 8813.71 Construction Inspection, Bridge Rehab: Route 22 ...
    #    The Expression of Interest due date is June 29, 2026, by 12 noon."
    # Parse each ad off that PIN -> description -> due-date shape.
    ad_re = re.compile(
        r"PIN#\s*([A-Za-z0-9.\-]+)\s+(.+?)\.?\s*"
        r"(?:The\s+)?Expression of Interest due date is\s+"
        r"([A-Z][a-z]+\s+\d{1,2},?\s*\d{4})",
        re.I)
    for m in ad_re.finditer(text):
        pin, desc, due_str = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        # Parse the due date; drop ads whose window has already closed.
        due_date = None
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                due_date = dt.datetime.strptime(due_str.replace(",", ""), fmt.replace(",", "")).date()
                break
            except ValueError:
                continue
        if due_date and due_date < today:
            continue
        # Trim overly long descriptions for a readable title.
        desc = re.sub(r"\s+", " ", desc)
        if len(desc) > 140:
            desc = desc[:137] + "..."
        title = f"{desc} (PIN {pin})"
        if pin in seen:
            continue
        seen.add(pin)
        out.append(result("NYSDOT", "NYSDOT", title, NYSDOT_URL,
                          due=due_str))

    print(f"[NYSDOT] {len(out)} ads")
    return out


# ─── NYS Contract Reporter ────────────────────────────────────────────────────
def fetch_nyscr() -> list[dict]:
    if not NYSCR_URL:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[NYSCR] beautifulsoup4 not installed — skipping.")
        return []
    try:
        r = requests.get(NYSCR_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[NYSCR] error: {e}")
        return []

    import re
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()

    # Each result card holds its title in a div whose `title` attribute starts
    # with "Full Title:". The CR# is in a sibling div under a "CR#:" label.
    title_divs = soup.find_all(
        "div", title=lambda t: bool(t) and t.strip().lower().startswith("full title:"))
    for d in title_divs:
        title = d.get_text(" ", strip=True)
        if not title or title in seen:
            continue
        # Keyword filter: the category URL filter alone lets non-engineering
        # ads through, so gate on our keywords like every other source.
        if not matches(title):
            continue
        seen.add(title)
        # Find the CR# in the NEAREST ancestor that actually contains one, so each
        # card gets its own number (walking a fixed number of levels up landed on a
        # shared container and gave every ad the first card's CR#).
        cr = ""
        card = d
        for _ in range(8):
            card = card.parent
            if card is None:
                break
            txt = card.get_text(" ", strip=True)
            if "CR#:" in txt:
                m = re.search(r"CR#:\s*(\d+)", txt)
                if m:
                    cr = m.group(1)
                break
        # Pull a due date from the card if one is shown (label varies), else Unknown.
        card_txt = ""
        c2 = d
        for _ in range(8):
            c2 = c2.parent
            if c2 is None:
                break
            if "CR#:" in c2.get_text(" ", strip=True):
                card_txt = c2.get_text(" ", strip=True)
                break
        dm = re.search(r"(?:due date|bid due|closing)[:\s]*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})",
                       card_txt, re.I)
        due = dm.group(1) if dm else "Unknown"
        # Link to a CR#-keyword search so it narrows to this one ad (the full ad
        # view still needs your NYSCR login, but this lands on the right record).
        link = (f"https://www.nyscr.ny.gov/Ads/Search?Status=All&Keyword={cr}"
                if cr else NYSCR_URL)
        out.append(result("NYS Contract Reporter", "NYS", title, link,
                          due=due, extra=f"CR# {cr}" if cr else ""))

    print(f"[NYSCR] {len(out)} ads")
    return out


# ─── Nassau County (APEX formal solicitation board, server-rendered) ──────────
def fetch_nassau() -> list[dict]:
    if not NASSAU_URL:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[Nassau] beautifulsoup4 not installed — skipping.")
        return []
    try:
        r = requests.get(NASSAU_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[Nassau] error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    # Columns: Link | Department | Solicitation# | Title | Status | Issue | End
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        dept = tds[1].get_text(" ", strip=True)
        sol = tds[2].get_text(" ", strip=True)
        title = tds[3].get_text(" ", strip=True)
        status = tds[4].get_text(" ", strip=True)
        end = tds[6].get_text(" ", strip=True)
        if status.lower() not in ("open", "amended"):
            continue
        if not title or not matches(title) or title in seen:
            continue
        seen.add(title)
        out.append(result(
            "Nassau County", f"Nassau — {dept}".strip(" —"), title,
            NASSAU_URL, due=end or "Unknown", extra=f"{sol} | {status}"))

    print(f"[Nassau] {len(out)} solicitations")
    return out


# ─── MTA Construction & Development ───────────────────────────────────────────
def fetch_mta() -> list[dict]:
    """Plain requests — this is the ORIGINAL approach, and it worked fine
    locally before any of this. The Akamai 403 only ever showed up from
    GitHub's cloud (Azure datacenter IP) runs. Once running on the
    self-hosted runner (your home IP), there's no reason this shouldn't
    work exactly like it always did — and a plain requests call has a much
    less distinctive network fingerprint than an automated Chromium browser,
    which is a more plausible thing for Akamai's bot detection to flag than
    a boring HTTP client. If this DOES get denied again from the self-hosted
    runner, that would mean Akamai is blocking on something requests can't
    fix either way, at which point Playwright wasn't the answer regardless."""
    if not MTA_URL:
        return []
    try:
        r = requests.get(MTA_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[MTA] error: {e}")
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[MTA] beautifulsoup4 not installed — skipping.")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    today = dt.date.today()
    out, seen = [], set()

    # Every block has a "Title/description:" field and a "Current opening/due
    # date:" field. Slice the page text block-by-block off those labels (bounded
    # by the next Title label) — robust against the nested link markup.
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n")]
    title_pat = re.compile(r"(?i)^title\s*/\s*desc(?:ription)?\s*:\s*(.*)$")
    idxs = [k for k, l in enumerate(lines) if title_pat.match(l)]
    for n, i in enumerate(idxs):
        end = idxs[n + 1] if n + 1 < len(idxs) else min(i + 24, len(lines))
        block = lines[i:end]
        desc = title_pat.match(lines[i]).group(1).strip()
        if not desc:  # label and value split across lines
            for l in block[1:]:
                if l:
                    desc = l
                    break
        if not desc or desc in seen:
            continue
        blocktext = " ".join(block)
        if "under review" in blocktext.lower():
            continue
        dm = re.search(r"due date[:\s]*([\d/]+)", blocktext, re.I)
        due = dm.group(1) if dm else ""
        d = _mdy_dates(due)
        if d and max(d) < today:          # past due
            continue
        if not matches(desc):
            continue
        seen.add(desc)
        out.append(result("MTA C&D", "MTA", desc, MTA_URL,
                          due=due or "Unknown"))

    print(f"[MTA] {len(out)} solicitations")
    if not out:
        preview = re.sub(r"\s+", " ", r.text).strip()[:300]
        print(f"[MTA] diag: response length={len(r.text)} chars, "
              f"title-block count={len(idxs)}")
        print(f"[MTA] diag: first 300 chars -> {preview!r}")
    return out


# ─── Email digest ─────────────────────────────────────────────────────────────
def build_html(results: list[dict]) -> str:
    if not results:
        return "<p>No matching RFx opportunities found this run.</p>"

    by_source: dict[str, list[dict]] = {}
    for r in results:
        by_source.setdefault(r["source"], []).append(r)

    new_total = sum(1 for r in results if r.get("is_new"))
    summary = (f"{len(results)} open opportunities across {len(by_source)} sources"
               + (f" &mdash; <b>{new_total} new since last run</b>." if new_total
                  else "."))
    parts = [f"<h2>RFx digest — {dt.date.today():%B %d, %Y}</h2>",
             f"<p>{summary}</p>"]

    badge = ("<span style='background:#d9480f;color:#fff;font-size:11px;"
             "font-weight:bold;padding:1px 5px;border-radius:3px;"
             "margin-right:6px;'>NEW</span>")

    # Colored status dot + label for the design-relevance classifier verdict.
    def status_tag(it: dict) -> str:
        rel = it.get("relevance")
        if rel == "relevant":
            color, label = "#2f9e44", "Potential contract"      # green
        elif rel == "not_relevant":
            color, label = "#e03131", "Unrelated"               # red
        elif rel == "unknown":
            color, label = "#f08c00", "Unknown"                 # yellow/amber
        else:
            return ""  # classifier didn't run at all
        reason = it.get("relevance_reason") or ""
        if it.get("relevance_title_only"):
            reason = (reason + " (title only)").strip() if reason else "title only"
        reason_html = f" <span style='color:#868e96'>— {reason}</span>" if reason else ""
        dot = (f"<span style='display:inline-block;width:9px;height:9px;"
               f"border-radius:50%;background:{color};margin-right:5px;"
               f"vertical-align:middle;'></span>")
        # Inline (no wrapping div) so it sits tight under the meta line — Outlook
        # adds big margins around block elements, which pushed it too far down.
        return (f"<br><span style='font-size:12px;'>{dot}"
                f"<b style='color:{color}'>{label}</b>{reason_html}</span>")

    import datetime as _dt

    def _due_sort_key(it: dict):
        """Parse an item's due date to a date for sorting. Handles the several
        formats sources emit (m/d/y, 'July 29, 2026', 'Jul 24th 2026', ISO).
        Items with no parseable due date sort to the BOTTOM."""
        raw = (it.get("due") or "").strip()
        if not raw or raw.lower() in ("unknown", "n/a"):
            return _dt.date.max
        # Strip ordinal suffixes (1st, 2nd, 3rd, 24th) and extra time text.
        cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", raw, flags=re.I)
        cleaned = cleaned.split(" at ")[0].strip()
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d %Y",
                    "%b %d, %Y", "%Y-%m-%d", "%B %d %Y"):
            try:
                return _dt.datetime.strptime(cleaned, fmt).date()
            except ValueError:
                continue
        # Last resort: find any m/d/y inside the string.
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
        if m:
            mm, dd, yy = (int(x) for x in m.groups())
            yy += 2000 if yy < 100 else 0
            try:
                return _dt.date(yy, mm, dd)
            except ValueError:
                pass
        return _dt.date.max

    # Sections alphabetical, but always push "Future RFPs" to the very bottom.
    def _section_order(kv):
        return (kv[0] == "Future RFPs", kv[0])
    for source, items in sorted(by_source.items(), key=_section_order):
        # Sort by soonest due date first; undated items fall to the bottom.
        # (NEW items keep their badge/highlight but no longer force to top —
        # due-date urgency is the primary ordering.)
        items.sort(key=_due_sort_key)
        n_new = sum(1 for x in items if x.get("is_new"))
        head = f"{source} ({len(items)}"
        head += f", {n_new} new)" if n_new else ")"
        parts.append(f"<h3 style='margin-bottom:4px'>{head}</h3>")
        parts.append("<ul style='margin-top:0'>")
        for it in items:
            link = (f"<a href='{it['url']}'>{it['title']}</a>"
                    if it["url"] else it["title"])
            meta = " &middot; ".join(x for x in [it["agency"], it["date"],
                                                 f"Due: {it.get('due', 'Unknown')}",
                                                 it["extra"]] if x)
            tag = badge if it.get("is_new") else ""
            style = (" style='background:#fff4e6;padding:2px 4px;'"
                     if it.get("is_new") else "")
            parts.append(f"<li{style}>{tag}{link}<br><small>{meta}"
                         f"{status_tag(it)}</small></li>")
        parts.append("</ul>")
    return "\n".join(parts)


def send_email(html: str, count: int) -> None:
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and RECIPIENT):
        print("[email] Gmail creds/recipient missing — printing instead:\n")
        print(html)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"RFx digest: {count} opportunities — {dt.date.today():%b %d}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_ADDRESS, [RECIPIENT], msg.as_string())
        print(f"[email] Sent digest with {count} items to {RECIPIENT}")
    except Exception as e:
        print(f"[email] send failed: {e}")


def _seen_key(r: dict) -> str:
    """Stable identity for an opportunity across runs."""
    return f"{r['source']}|{r['title'].strip().lower()}"


def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(keys: set) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(keys), f)
    except Exception as e:
        print(f"[seen] could not save {SEEN_FILE}: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    print(f"=== RFx run {dt.datetime.now():%Y-%m-%d %H:%M} "
          f"(lookback {LOOKBACK_DAYS}d) ===")
    all_results: list[dict] = []
    all_results += fetch_samgov()
    all_results += fetch_crol()
    all_results += fetch_njdot()
    all_results += fetch_nysdot()
    all_results += fetch_nyscr()
    all_results += fetch_nassau()
    all_results += fetch_mta()

    if INCLUDE_PLAYWRIGHT:
        try:
            from rfx_playwright_sources import get_playwright_results
            all_results += get_playwright_results()
        except Exception as e:
            print(f"[playwright] module error (continuing without it): {e}")

    # Suffolk town/village portals (Brookhaven, Huntington, Smithtown static;
    # Babylon via Playwright). Islip excluded — login wall.
    try:
        from rfx_suffolk_towns import get_all_suffolk_town_results
        all_results += get_all_suffolk_town_results()
    except Exception as e:
        print(f"[suffolk towns] module error (continuing without it): {e}")

    # Upcoming / not-yet-advertised opportunities (heads-up section, sorts last).
    try:
        from rfx_future import get_future_results, get_current_results
        all_results += get_current_results()   # NYC DOT currently-open RFPs
        all_results += get_future_results()    # NYSDOT prelim + NYC DOT future
    except Exception as e:
        print(f"[future] module error (continuing without it): {e}")

    # Final dedup by (title, url).
    seen, deduped = set(), []
    for r in all_results:
        key = (r["title"].lower(), r["url"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    print(f"=== {len(deduped)} unique matches ===")

    # Flag which are new since last run, but always send the full current list.
    previously = load_seen()
    baseline = len(previously) == 0
    new_count = 0
    for r in deduped:
        # On the very first run there's no history, so nothing is flagged "new"
        # (otherwise everything would be). After that, new = not seen before.
        r["is_new"] = (not baseline) and (_seen_key(r) not in previously)
        if r["is_new"]:
            new_count += 1
    save_seen(previously | {_seen_key(r) for r in deduped})

    if baseline:
        print(f"=== baselining {len(deduped)} items (new flagged from next run) ===")
    else:
        print(f"=== {new_count} NEW, {len(deduped)} total open ===")

    # Optional LLM relevance pass (OpenRouter). Runs after seen-tracking so it
    # only affects the email display. Fails open — any error keeps everything.
    try:
        from rfx_llm_filter import llm_filter
        deduped = llm_filter(deduped)
    except Exception as e:
        print(f"[llm] module error (continuing without it): {e}")

    # Optional design-vs-construction classifier (OpenRouter). Fetches each
    # opportunity's detail page and tags likely-construction bids with a note
    # (never removes them). Fails open — any error leaves items unannotated.
    try:
        from rfx_classify import classify_results
        deduped = classify_results(deduped)
    except Exception as e:
        print(f"[classify] module error (continuing without it): {e}")

    send_email(build_html(deduped), len(deduped))


if __name__ == "__main__":
    main()
