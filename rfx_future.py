"""
rfx_future.py
Upcoming / not-yet-advertised opportunities — a "Future RFPs" heads-up section.

Sources:
  - NYSDOT Preliminary Advertisements
    https://www.dot.ny.gov/doing-business/opportunities/eng-preliminaryad
  - NYC DOT Future RFPs
    https://a841-dotwebpcard01.nyc.gov/RFP/Home/Future

These are opportunities that HAVEN'T been formally advertised yet — they give
lead time to position/team before the real solicitation drops. They flow into
the digest under a "Future RFPs" section, same format as every other source,
and pass through the same keyword filter and (if enabled) the classifier.

Because these are pre-advertisement, most won't have a firm "due" date — they
carry an ESTIMATED RELEASE date instead, shown in the due field labeled as est.
"""

import re
import datetime as dt

import requests

from rfx_common import matches, result

SOURCE = "Future RFPs"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RFxMonitor/1.0"}

NYSDOT_PRELIM_URL = ("https://www.dot.ny.gov/doing-business/opportunities/"
                     "eng-preliminaryad")
NYCDOT_FUTURE_URL = "https://a841-dotwebpcard01.nyc.gov/RFP/Home/Future"
NYCDOT_CURRENT_URL = "https://a841-dotwebpcard01.nyc.gov/RFP/Home/Current"


# ─── NYSDOT Preliminary Advertisements ────────────────────────────────────────
def fetch_nysdot_prelim() -> list[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[Future/NYSDOT] beautifulsoup4 not installed — skipping.")
        return []
    try:
        r = requests.get(NYSDOT_PRELIM_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[Future/NYSDOT] error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    out, seen = [], set()

    # Preliminary ads follow: "<Month DD, YYYY> D# D0NNNNN / PIN# <pin>
    # <description>. Expected to be <funding> funded."  Anchor on the D#/PIN#
    # pair (nav/breadcrumb text lacks it) and stop the description at "Expected
    # to be" or the next "D#", so rows don't bleed into each other.
    ad_re = re.compile(
        r"(?:([A-Z][a-z]+ \d{1,2},? \d{4})\s+)?"      # optional posting/ad date
        r"D#\s*([A-Za-z0-9]+)\s*/\s*PIN#\s*([A-Za-z0-9.\-]+)\s+"
        r"(.+?)"                                        # description (lazy)
        r"(?=\.?\s*Expected to be|\s+D#\s|$)",
        re.I)
    for m in ad_re.finditer(text):
        ad_date, dnum, pin, desc = (g.strip() if g else "" for g in m.groups())
        desc = re.sub(r"\s+", " ", desc).strip(" .")
        if not desc or len(desc) < 8:
            continue
        if pin in seen:
            continue
        seen.add(pin)
        if len(desc) > 150:
            desc = desc[:147] + "..."
        title = f"{desc} (PIN {pin})"
        if matches(title):
            out.append(result(SOURCE, "NYSDOT (Preliminary)", title,
                              NYSDOT_PRELIM_URL,
                              due=f"Ad ~{ad_date}" if ad_date else ""))

    print(f"[Future/NYSDOT] {len(out)} upcoming")
    return out


# ─── NYC DOT Future RFPs ──────────────────────────────────────────────────────
def fetch_nycdot_future() -> list[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[Future/NYCDOT] beautifulsoup4 not installed — skipping.")
        return []
    try:
        r = requests.get(NYCDOT_FUTURE_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[Future/NYCDOT] error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()

    # Clean table: Project ID | Description | Type of Service | Est. Release |
    # Solicitation Method. Row order matches those columns.
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 4:
            continue
        proj_id, desc, svc, release = cells[0], cells[1], cells[2], cells[3]
        if not desc or desc.lower() == "project description":
            continue  # header row
        title = f"{desc}" + (f" [{svc}]" if svc else "")
        key = proj_id or desc
        if key in seen:
            continue
        # The entire NYC DOT Future RFP page is transportation/bridge design
        # work (TD = Total Design, CSS = Construction Support Services, REI =
        # Resident Engineering Inspection — all in scope, and "BR" = bridge).
        # So list every row rather than keyword-filtering, which would drop
        # abbreviated titles like "21st Ave RR BR".
        seen.add(key)
        out.append(result(SOURCE, "NYC DOT (Future)", title,
                          NYCDOT_FUTURE_URL,
                          due=f"Est. release {release}" if release else "",
                          extra=proj_id))

    print(f"[Future/NYCDOT] {len(out)} upcoming")
    return out


# ─── NYC DOT Current RFPs (open now, own PCARD portal) ────────────────────────
# Same table format as the Future page. Per NYC DOT: "RFPs are removed from this
# webpage the day after the RFP due date" — so everything here is genuinely
# open. Tagged as a normal current source (NOT the Future heads-up section),
# so these land in a dated "NYC DOT" section like any other live opportunity.

def fetch_nycdot_current() -> list[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[NYCDOT Current] beautifulsoup4 not installed — skipping.")
        return []
    try:
        r = requests.get(NYCDOT_CURRENT_URL, headers=HEADERS, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"[NYCDOT Current] error: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()

    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 4:
            continue
        proj_id, desc, svc = cells[0], cells[1], cells[2]
        # The Current page carries a Due Date column; grab the last date-looking
        # cell as the due date (columns can shift slightly vs. Future).
        due = ""
        for c in cells[3:]:
            if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", c):
                due = c
        if not desc or desc.lower() == "project description":
            continue  # header row
        title = f"{desc}" + (f" [{svc}]" if svc else "")
        key = proj_id or desc
        if key in seen:
            continue
        # Entire page is NYC DOT transportation/bridge design work — list all.
        seen.add(key)
        out.append(result("NYC DOT", "NYC DOT", title, NYCDOT_CURRENT_URL,
                          due=due or "Unknown", extra=proj_id))

    print(f"[NYCDOT Current] {len(out)} open")
    return out


def get_future_results() -> list[dict]:
    """Called from rfx_alert.py — combined upcoming opportunities."""
    return fetch_nysdot_prelim() + fetch_nycdot_future()


def get_current_results() -> list[dict]:
    """Called from rfx_alert.py — NYC DOT's currently-advertised RFPs."""
    return fetch_nycdot_current()


if __name__ == "__main__":
    rows = get_future_results()
    print(f"\nTotal future opportunities: {len(rows)}\n")
    for r in rows:
        print(f"[{r['agency']}] {r['title']}")
        print(f"   {r.get('due','')}\n   {r['url']}\n")
