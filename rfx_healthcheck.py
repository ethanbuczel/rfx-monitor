"""
rfx_healthcheck.py — on-demand health check for every scraped source.

The problem it solves: a broken scraper and a genuinely quiet source BOTH
return zero opportunities, so you can't tell them apart from the digest. This
checks whether each source's page still LOADS and has the STRUCTURE we expect
(rows / table / links / JSON), regardless of whether anything matched keywords.

Three outcomes per source:
  OK          fetched + found the expected structure (even if 0 keyword matches)
  SUSPICIOUS  fetched, but the structure looks empty/changed (worth a look)
  BROKEN      fetch failed — error, timeout, non-200, block, or login wall

It emails a status report. Run it on demand from its own workflow.

Note: a few sources need a real browser (Playwright) — those are probed with a
lighter "does the page load and mention opportunities" check rather than a full
scrape, to keep the health check fast.
"""

import os
import re
import smtplib
import datetime as dt
from email.mime.text import MIMEText

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT = (os.environ.get("RFX_RECIPIENT", "") or GMAIL_ADDRESS)
SAM_API_KEY = os.environ.get("SAM_API_KEY", "")

OK, SUS, BROKEN = "OK", "SUSPICIOUS", "BROKEN"


def _req(url, timeout=30, **kw):
    return requests.get(url, headers=HEADERS, timeout=timeout, **kw)


# Each checker returns (status, detail). A checker does a lightweight fetch and
# looks for the structural signal that proves the page is intact.
def check_samgov():
    if not SAM_API_KEY:
        return SUS, "no SAM_API_KEY set"
    url = ("https://api.sam.gov/prod/opportunities/v2/search"
           f"?api_key={SAM_API_KEY}&postedFrom="
           f"{(dt.date.today()-dt.timedelta(days=7)):%m/%d/%Y}"
           f"&postedTo={dt.date.today():%m/%d/%Y}&ncode=541330&limit=1&offset=0")
    try:
        r = _req(url, timeout=40)
        if r.status_code != 200:
            return BROKEN, f"HTTP {r.status_code} (key may be expired)"
        data = r.json()
        if "totalRecords" in data:
            return OK, f"API live, {data.get('totalRecords','?')} total records"
        return SUS, "200 but no totalRecords field (API shape changed?)"
    except Exception as e:
        return BROKEN, f"{type(e).__name__}: {e}"


def check_html(url, must_contain=None, min_rows=1, row_sel=None, label=""):
    """Generic: fetch an HTML page, confirm it loaded and has expected content."""
    try:
        r = _req(url)
        if r.status_code != 200:
            return BROKEN, f"HTTP {r.status_code}"
        body = r.text
        if len(body) < 500:
            return SUS, f"tiny response ({len(body)} chars) — shell/block?"
        low = body.lower()
        if any(x in low for x in ("access denied", "captcha",
                                  "are you a robot", "cloudflare")):
            return BROKEN, "looks blocked (captcha/cloudflare)"
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(body, "html.parser")
            rows = soup.find_all("tr") if not row_sel \
                else soup.select(row_sel)
        except Exception:
            rows = re.findall(r"<tr", body)
        # content check
        if must_contain and must_contain.lower() not in low:
            return SUS, f"loaded but missing expected text '{must_contain}'"
        if len(rows) >= min_rows:
            return OK, f"loaded, {len(rows)} rows/elements"
        return SUS, f"loaded but only {len(rows)} rows (structure changed?)"
    except Exception as e:
        return BROKEN, f"{type(e).__name__}: {e}"


def check_mta():
    """MTA sits behind Akamai bot detection. Rather than do a separate probe
    (which Akamai may 403 differently than the real scraper), call the ACTUAL
    production fetch_mta() and judge by what it returns. This tests the real
    code path, so a green here means the real pipeline can reach MTA."""
    try:
        from rfx_alert import fetch_mta
        rows = fetch_mta()
        # fetch_mta prints "[MTA] error: ..." and returns [] on failure. A
        # non-empty list means it reached and parsed the page. An empty list is
        # ambiguous (could be genuinely no matches), so do a bare fetch to tell
        # "reached but 0 matches" from "blocked".
        if rows:
            return OK, f"fetch_mta() returned {len(rows)} solicitations"
        # empty — probe the raw page to distinguish quiet vs blocked
        import rfx_alert
        r = _req(rfx_alert.MTA_URL, timeout=40)
        if r.status_code == 200:
            return OK, "page reachable (fetch_mta returned 0 — quiet)"
        if r.status_code in (403, 429):
            return SUS, (f"HTTP {r.status_code} Akamai block — if the real "
                         f"digest still gets MTA items, ignore; if not, MTA is "
                         f"blocking this runner's IP")
        return SUS, f"HTTP {r.status_code}"
    except Exception as e:
        return BROKEN, f"{type(e).__name__}: {e}"


def check_via_fetch(fn_name, module="rfx_alert", label=""):
    """Check a source by calling its real production fetch function. Reaching +
    parsing the page (returning a list without erroring) = OK. This tests the
    actual code path, so it can't disagree with the real pipeline."""
    try:
        mod = __import__(module)
        fn = getattr(mod, fn_name)
        rows = fn()
        if rows:
            return OK, f"{fn_name}() returned {len(rows)} items"
        return OK, f"{fn_name}() ran cleanly (0 items — quiet)"
    except Exception as e:
        return BROKEN, f"{fn_name}() failed: {type(e).__name__}: {e}"


def check_json(url, key=None):
    try:
        r = _req(url, timeout=40)
        if r.status_code != 200:
            return BROKEN, f"HTTP {r.status_code}"
        data = r.json()
        n = len(data) if isinstance(data, list) else "?"
        return OK, f"JSON live ({n} records)"
    except Exception as e:
        return BROKEN, f"{type(e).__name__}: {e}"


def check_playwright(url, must_contain=None, label=""):
    """Lighter probe for JS sources: load in a browser, confirm content."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return SUS, "playwright not installed"
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            pg = b.new_context(user_agent=HEADERS["User-Agent"]).new_page()
            pg.goto(url, timeout=35000, wait_until="domcontentloaded")
            try:
                pg.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            pg.wait_for_timeout(2500)
            body = pg.content()
            b.close()
        if len(body) < 1000:
            return SUS, f"tiny render ({len(body)} chars)"
        if "please register" in body.lower() or "log in to access" in body.lower():
            return BROKEN, "login wall"
        if must_contain and must_contain.lower() not in body.lower():
            return SUS, f"rendered but missing '{must_contain}'"
        return OK, f"rendered ({len(body)} chars)"
    except Exception as e:
        return BROKEN, f"{type(e).__name__}: {e}"


# ---- the source list: (name, checker callable) --------------------------------
def build_checks():
    return [
        ("SAM.gov", lambda: check_samgov()),
        ("NYC CROL", lambda: check_json(
            "https://data.cityofnewyork.us/resource/dg92-zbpx.json?$limit=1")),
        ("NYSDOT detail ads", lambda: check_html(
            "https://www.dot.ny.gov/doing-business/opportunities/eng-detailad",
            min_rows=1)),
        ("NYSDOT preliminary", lambda: check_html(
            "https://www.dot.ny.gov/doing-business/opportunities/eng-preliminaryad",
            min_rows=1)),
        ("NYSDOT design-build", lambda: check_html(
            "https://www.dot.ny.gov/main/business-center/"
            "potential-design-build-projects", min_rows=0,
            must_contain="design")),
        ("NYS Contract Reporter", lambda: check_via_fetch("fetch_nyscr")),
        ("Nassau County", lambda: check_html(
            "https://apex5.nassaucountyny.gov/ords/f?p=533:226", min_rows=1)),
        ("MTA C&D", lambda: check_mta()),
        ("NYC DOT current", lambda: check_html(
            "https://a841-dotwebpcard01.nyc.gov/RFP/Home/Current", min_rows=1)),
        ("NYC DOT future", lambda: check_html(
            "https://a841-dotwebpcard01.nyc.gov/RFP/Home/Future", min_rows=1)),
        # Playwright / JS sources
        ("PANYNJ Bonfire", lambda: check_playwright(
            "https://panynj.bonfirehub.com/opportunities",
            must_contain="opportunit")),
        ("Suffolk County Bonfire", lambda: check_playwright(
            "https://suffolkcountyny.bonfirehub.com/portal/"
            "?tab=openOpportunities", must_contain="opportunit")),
        ("NYC PASSPort", lambda: check_playwright(
            "https://passport.cityofnewyork.us/page.aspx/en/rfp/"
            "request_browse_public")),
        ("NYC EDC", lambda: check_playwright("https://edc.nyc/rfps")),
        ("SUNY (JAGGAER)", lambda: check_playwright(
            "https://bids.sciquest.com/apps/Router/PublicEvent"
            "?CustomerOrg=SUNY")),
        # BidNet-hosted (public listing pages)
        ("Suffolk County (BidNet)", lambda: check_html(
            "https://www.bidnetdirect.com/new-york/suffolkcounty", min_rows=0,
            must_contain="solicitation")),
        ("Brookhaven (BidNet)", lambda: check_html(
            "https://www.bidnetdirect.com/new-york/townofbrookhaven",
            min_rows=0, must_contain="solicitation")),
        ("Westchester County portal", lambda: check_html(
            "https://rfp.westchestergov.com/rfp/rfps", min_rows=0,
            must_contain="request")),
        # Town sites
        ("Smithtown (RSS)", lambda: check_html(
            "https://www.smithtownny.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
            min_rows=0, must_contain="rss")),
        ("Huntington", lambda: check_html(
            "https://www.huntingtonny.gov/Bids/RFPs", min_rows=0)),
        ("Hempstead", lambda: check_html(
            "https://hempsteadny.gov/Bids.aspx", min_rows=0)),
        ("Shelter Island", lambda: check_html(
            "https://www.shelterislandtown.gov/Bids.aspx", min_rows=0)),
    ]


def run():
    print(f"=== RFx Health Check {dt.datetime.now():%Y-%m-%d %H:%M} ===\n")
    results = []
    for name, checker in build_checks():
        try:
            status, detail = checker()
        except Exception as e:
            status, detail = BROKEN, f"checker crashed: {e}"
        results.append((name, status, detail))
        icon = {"OK": "[OK]  ", "SUSPICIOUS": "[WARN]", "BROKEN": "[FAIL]"}[status]
        print(f"{icon} {name:28} {detail}")

    n_ok = sum(1 for _, s, _ in results if s == OK)
    n_sus = sum(1 for _, s, _ in results if s == SUS)
    n_bad = sum(1 for _, s, _ in results if s == BROKEN)
    print(f"\n{n_ok} OK, {n_sus} suspicious, {n_bad} broken, "
          f"of {len(results)} sources.")

    _email(results, n_ok, n_sus, n_bad)
    return results


def _email(results, n_ok, n_sus, n_bad):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and RECIPIENT):
        print("\n[email] creds/recipient missing — not sending.")
        return
    color = {"OK": "#2E7D32", "SUSPICIOUS": "#C77D1E", "BROKEN": "#C0392B"}
    rows_html = ""
    # broken + suspicious first so problems are up top
    order = {"BROKEN": 0, "SUSPICIOUS": 1, "OK": 2}
    for name, status, detail in sorted(results, key=lambda r: order[r[1]]):
        rows_html += (
            f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>"
            f"<b style='color:{color[status]}'>{status}</b></td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{name}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;"
            f"color:#666'>{detail}</td></tr>")
    headline = ("All sources healthy" if n_bad == 0 and n_sus == 0
                else f"{n_bad} broken, {n_sus} suspicious — needs attention")
    hcolor = "#2E7D32" if (n_bad == 0 and n_sus == 0) else "#C0392B"
    html = (
        f"<div style='font-family:Arial,sans-serif'>"
        f"<h2 style='color:{hcolor}'>RFx Source Health: {headline}</h2>"
        f"<p style='color:#666'>{n_ok} OK &middot; {n_sus} suspicious &middot; "
        f"{n_bad} broken, of {len(results)} sources &middot; "
        f"{dt.datetime.now():%Y-%m-%d %H:%M}</p>"
        f"<table style='border-collapse:collapse;font-size:14px'>{rows_html}"
        f"</table>"
        f"<p style='color:#999;font-size:12px;margin-top:16px'>"
        f"OK = page loaded with expected structure. SUSPICIOUS = loaded but "
        f"structure looks off (may have changed). BROKEN = fetch failed. "
        f"A SUSPICIOUS/BROKEN source may be silently returning no results in "
        f"your digest.</p></div>")
    msg = MIMEText(html, "html")
    msg["Subject"] = (f"RFx Health Check: {headline}")
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_ADDRESS, [RECIPIENT], msg.as_string())
        print(f"\n[email] Sent health report to {RECIPIENT}")
    except Exception as e:
        print(f"\n[email] send failed: {e}")


if __name__ == "__main__":
    run()
