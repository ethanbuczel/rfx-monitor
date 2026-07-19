"""
rfx_suffolk_towns.py
Scrapers for Suffolk County town procurement sites.

Western towns:
  - Town of Brookhaven     (BidNet Direct — static HTML)
  - Town of Huntington     (own site — static HTML)
  - Town of Smithtown      (News Flash RSS feed — static XML)
  - Town of Babylon        (Bid Portal app — JS-rendered, Playwright)

East End towns:
  - Town of Riverhead      (CivicEngage Bids.aspx — static HTML)
  - Town of East Hampton   (CivicEngage Bids.aspx — static HTML)
  - Town of Southold       (CivicEngage Bids.aspx — static HTML)
  - Town of Shelter Island (CivicEngage Bids.aspx — static HTML)
  - Town of Southampton    (ProcureWare portal — JS-rendered, Playwright)

NOT covered:
  - Town of Islip — login-only portal (bids.islipny.gov); the old public
    site (bids.townofislip-ny.gov) is a dead domain (DNS does not resolve).
    If Islip matters: register a free vendor account there for email alerts.

Filtering: uses the SHARED keyword filter from rfx_common (same as MTA,
NYSDOT, etc.), so tuning the keyword list in one place covers the towns too.
Results use the shared result() shape, so the main digest's dedupe and
NEW-tracking treat these like every other source.

SETUP:
  pip install requests beautifulsoup4 feedparser playwright
  python -m playwright install chromium

Usage:
  from rfx_suffolk_towns import get_all_suffolk_town_results
  results = get_all_suffolk_town_results()   # synchronous, returns combined list
"""

import asyncio
import re
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from rfx_common import matches, result

SOURCE = "Suffolk Towns"          # digest section for Suffolk town bids
SOURCE_NASSAU = "Nassau Towns"    # digest section for Nassau town bids
# (agency = the specific town, shown under whichever section)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


# ─── 1. Town of Brookhaven (BidNet Direct, static) ────────────────────────────

def scrape_suffolk_county() -> list[dict]:
    """Suffolk County's OPEN bids live on BidNet / Empire State Purchasing
    Group (same platform as Brookhaven), NOT on the county's own site. The
    old Playwright scraper hit an informational DPW page and grabbed nav links
    like 'Highway Maintenance' — this replaces it with the real bid feed."""
    out = []
    url = "https://www.bidnetdirect.com/new-york/suffolkcounty"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for row in soup.find_all("tr"):
            link = row.find("a", href=True)
            if not link:
                continue
            title = link.get_text(strip=True)
            href = link["href"]
            if href.startswith("/"):
                href = "https://www.bidnetdirect.com" + href
            if not title or len(title) < 5:
                continue
            if matches(title):
                row_text = row.get_text(" ", strip=True)
                due = "Unknown"
                dm = re.search(
                    r"clos\w*(?:\s+date)?\s*[:\-]?\s*"
                    r"(\d{1,2}/\d{1,2}/\d{2,4}"
                    r"|[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})",
                    row_text, re.I)
                if dm:
                    due = dm.group(1)
                out.append(result("Suffolk County", "Suffolk County DPW",
                                  title, href, due=due, extra=row_text[:120]))
    except Exception as e:
        print(f"[Suffolk County] Error: {e}")
    return out


def scrape_brookhaven() -> list[dict]:
    out = []
    url = "https://www.bidnetdirect.com/new-york/townofbrookhaven"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Bid rows are table rows with a solicitation link
        for row in soup.find_all("tr"):
            link = row.find("a", href=True)
            if not link:
                continue
            title = link.get_text(strip=True)
            href = link["href"]
            if href.startswith("/"):
                href = "https://www.bidnetdirect.com" + href
            if not title or len(title) < 5:
                continue
            if matches(title):
                row_text = row.get_text(" ", strip=True)
                # BidNet rows include the closing date in the row text, e.g.
                # "...Closing 07/14/2026" — pull it into the due field so the
                # digest doesn't show "Unknown". Accept a couple date formats.
                due = "Unknown"
                dm = re.search(
                    r"clos\w*(?:\s+date)?\s*[:\-]?\s*"
                    r"(\d{1,2}/\d{1,2}/\d{2,4}"
                    r"|[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})",
                    row_text, re.I)
                if dm:
                    due = dm.group(1)
                out.append(result(SOURCE, "Town of Brookhaven", title, href,
                                  due=due, extra=row_text[:120]))
    except Exception as e:
        print(f"[Brookhaven] Error: {e}")
    return out


# ─── 2. Town of Huntington (own site, static) ─────────────────────────────────

def scrape_huntington() -> list[dict]:
    out = []
    url = "https://www.huntingtonny.gov/Bids/RFPs"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            href = link["href"]
            # Huntington bid links contain "bid=" in the query string
            if "bid=" not in href.lower() and "rfp" not in href.lower():
                continue
            if not text or len(text) < 5:
                continue
            if matches(text):
                # Due date is embedded in the title text, e.g. "(Due Date: 07/17/26)"
                out.append(result(SOURCE, "Town of Huntington", text, href))
    except Exception as e:
        print(f"[Huntington] Error: {e}")
    return out


# ─── 3. Town of Smithtown (News Flash RSS feed, static) ───────────────────────

def scrape_smithtown() -> list[dict]:
    """Smithtown posts bids to its CivicPlus 'News Flash' feed. The page itself
    is JS-rendered, but the RSS feed is clean XML — parse that instead."""
    import feedparser
    out = []
    feed_url = "https://www.smithtownny.gov/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml"
    try:
        feed = feedparser.parse(feed_url, request_headers=HEADERS)
        for entry in feed.entries:
            title = entry.get("title", "").strip()
            link = entry.get("link", feed_url)
            summary = entry.get("summary", "")
            if title and matches(title, summary):
                out.append(result(SOURCE, "Town of Smithtown", title, link,
                                  date=entry.get("published", "")))
    except Exception as e:
        print(f"[Smithtown] Error: {e}")
    return out


# ─── 4. CivicEngage towns (Riverhead, East Hampton, Southold, Shelter Island) ─
# All four use the identical CivicPlus "Bid Postings" module at /Bids.aspx,
# server-rendered. Bid items are links whose href contains "bidID=".

CIVICENGAGE_TOWNS = [
    ("Town of Riverhead",      "https://www.townofriverheadny.gov/bids.aspx", SOURCE),
    ("Town of East Hampton",   "https://ehamptonny.gov/Bids.aspx", SOURCE),
    ("Town of Southold",       "https://www.southoldtownny.gov/Bids.aspx", SOURCE),
    ("Town of Shelter Island", "https://www.shelterislandtown.gov/Bids.aspx", SOURCE),
    ("Town of Hempstead",      "https://hempsteadny.gov/Bids.aspx", SOURCE_NASSAU),
]


def scrape_civicengage_town(town_name: str, url: str, source: str = SOURCE) -> list[dict]:
    out = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        base = url.split("/Bids.aspx")[0].split("/bids.aspx")[0]
        seen_ids = set()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "bidid=" not in href.lower():
                continue
            title = link.get_text(strip=True)
            # Skip "Read on" style helper links and dupes of the same bid
            if not title or len(title) < 8:
                continue
            bid_id = href.lower().split("bidid=")[1].split("&")[0]
            if bid_id in seen_ids:
                continue
            if not href.startswith("http"):
                href = base + ("/" if not href.startswith("/") else "") + href.lstrip("/")
            if matches(title):
                seen_ids.add(bid_id)
                out.append(result(source, town_name, title, href))
    except Exception as e:
        print(f"[{town_name}] Error: {e}")
    return out


def scrape_civicengage_all() -> list[dict]:
    out = []
    for name, url, source in CIVICENGAGE_TOWNS:
        out += scrape_civicengage_town(name, url, source)
    return out


# ─── Town of Oyster Bay (WordPress "Doing Business" page, static) ─────────────
# The page carries two HTML tables: "Available Bids" (bid no. -> PDF link,
# description, due/opening date) and "RFPs for Professional Services".

def scrape_oyster_bay() -> list[dict]:
    out = []
    url = "https://oysterbaytown.com/doing-business-with-the-town/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        import datetime as _dt
        today = _dt.date.today()
        date_pat = re.compile(r"([A-Z][a-z]{2,8})\s+(\d{1,2}),?\s+(\d{4})")

        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            link = row.find("a", href=True)
            href = link["href"] if link else url
            if href.startswith("/"):
                href = "https://oysterbaytown.com" + href

            # Bids table: [bid_no, description, due/opening] — RFP table:
            # [rfp title, department, release, due]. Due date is the LAST
            # date in the row; title is the longest descriptive cell.
            full = " ".join(texts)
            dm = date_pat.findall(full)
            due = f"{dm[-1][0]} {dm[-1][1]}, {dm[-1][2]}" if dm else ""
            # Skip rows whose due date already passed
            if due:
                try:
                    d = _dt.datetime.strptime(due, "%B %d, %Y").date()
                    if d < today:
                        continue
                except ValueError:
                    pass
            title = max(texts, key=len).strip()
            title = date_pat.sub("", title).strip(" -–·")
            if not title or len(title) < 8:
                continue
            bid_no = texts[0][:40] if texts[0] != title else ""
            if matches(title):
                out.append(result(SOURCE_NASSAU, "Town of Oyster Bay", title, href,
                                  due=due or "Unknown", extra=bid_no))
    except Exception as e:
        print(f"[Oyster Bay] Error: {e}")
    return out


# ─── Town of Islip (login portal, Playwright + stored credentials) ────────────
# bids.islipny.gov requires a (free) vendor account even to VIEW the bid list.
# Register at https://bids.islipny.gov -> Create an Account, then provide the
# credentials via environment variables (locally with setx, on GitHub as
# repository secrets — never in code):
#   ISLIP_EMAIL     your registered email
#   ISLIP_PASSWORD  your portal password
# If the credentials are absent, this source skips itself silently.
# Post-login page structure is a first pass — run with DIAG=1 if it returns 0
# to dump what the logged-in page actually shows.

async def scrape_islip(pw, diag: bool = False) -> list[dict]:
    import os as _os
    email = _os.environ.get("ISLIP_EMAIL", "")
    password = _os.environ.get("ISLIP_PASSWORD", "")
    if not email or not password:
        print("[Islip] ISLIP_EMAIL/ISLIP_PASSWORD not set — skipping.")
        return []

    out = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(user_agent=HEADERS["User-Agent"])
    page = await ctx.new_page()
    try:
        await page.goto("https://bids.islipny.gov/", timeout=35000,
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # Log in (AJAX auth -> authenticateAPI.php sets a session cookie).
        email_box = await page.query_selector("input[type='email']")
        pass_box = await page.query_selector("input[type='password']")
        if not (email_box and pass_box):
            print("[Islip] login form not found — skipping.")
            return []

        auth_ok = {"v": False}

        async def _on_response(resp):
            if "authenticate" in resp.url.lower():
                try:
                    import json as _json
                    data = _json.loads(await resp.text())
                    auth_ok["v"] = str(data.get("status")) == "1"
                except Exception:
                    pass
        page.on("response", lambda r: asyncio.create_task(_on_response(r)))

        await email_box.click()
        await email_box.type(email, delay=25)
        await pass_box.click()
        await pass_box.type(password, delay=25)
        btn = await page.query_selector("button[type='submit'], "
                                        "button:has-text('Login')")
        if btn:
            await btn.click()
        else:
            await pass_box.press("Enter")
        await page.wait_for_timeout(3500)

        if not auth_ok["v"]:
            print("[Islip] login failed — check ISLIP_EMAIL/ISLIP_PASSWORD.")
            return []

        # After login, the Bids and RFP/RFQ tabs load bidView links.
        # ?page=main&type=bids and &type=rfp are the two listings.
        seen_ids = set()
        for list_url in ("https://bids.islipny.gov/?page=main&type=bids",
                         "https://bids.islipny.gov/?page=main&type=rfp"):
            try:
                await page.goto(list_url, timeout=25000,
                                wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
            except Exception:
                continue

            for link in await page.query_selector_all("a[href*='bidView']"):
                text = (await link.inner_text()).strip()
                href = await link.get_attribute("href") or ""
                m = re.search(r"id=(\d+)", href)
                bid_id = m.group(1) if m else href
                if bid_id in seen_ids:
                    continue
                if not href.startswith("http"):
                    href = "https://bids.islipny.gov/" + href.lstrip("/")
                # First line of the link text is the bid title; the rest is
                # posted/closes dates and description.
                title = text.split("\n")[0].strip()
                # Pull the closing date if present.
                dm = re.search(r"closes?:\s*(\d{1,2}/\d{1,2}/\d{2,4})",
                               text, re.I)
                due = dm.group(1) if dm else "Unknown"
                if title and matches(text):
                    seen_ids.add(bid_id)
                    out.append(result(SOURCE, "Town of Islip", title, href,
                                      due=due))

        if diag:
            print(f"[Islip DIAG] logged in OK, harvested {len(out)} matches")

    except PWTimeout:
        print("[Islip] Timed out")
    except Exception as e:
        print(f"[Islip] Error: {e}")
    finally:
        await browser.close()
    print(f"[Islip] {len(out)} matches")
    return out


# ─── Town of North Hempstead (Revize CMS bids page, static) ───────────────────
# Rows pair a bid-number link (usually a PDF) with a description and two dates.
# The page lists both open and long-closed bids, so we keep only rows whose Due
# Date is today or later.

def scrape_north_hempstead() -> list[dict]:
    out = []
    url = "https://www.northhempsteadny.gov/departments/purchasing/bids___rfps/index.php"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        import datetime as _dt
        today = _dt.date.today()

        for li in soup.find_all("li"):
            link = li.find("a", href=True)
            if not link:
                continue
            full = li.get_text(" ", strip=True)
            # Real bid rows carry release+due dates; nav-menu <li> blobs don't.
            # Requiring 2+ dates (and a sane length) excludes the site menus.
            dates = re.findall(r"[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}", full)
            if len(dates) < 2 or len(full) > 400:
                continue
            bid_no = link.get_text(strip=True)
            desc = full
            if bid_no and full.startswith(bid_no):
                desc = full[len(bid_no):].strip()
            due = dates[-1]
            # Skip closed bids (due date in the past)
            if due:
                try:
                    d = _dt.datetime.strptime(due, "%B %d, %Y").date()
                    if d < today:
                        continue
                except ValueError:
                    pass
            # Title = the description with dates/times stripped off the end.
            title = re.split(r"\s+[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}", desc)[0].strip()
            if not title or len(title) < 6:
                continue
            href = link["href"]
            if href.startswith("/"):
                href = "https://www.northhempsteadny.gov" + href
            elif not href.startswith("http"):
                href = "https://www.northhempsteadny.gov/" + href
            if matches(title):
                out.append(result(SOURCE_NASSAU, "Town of North Hempstead", title, href,
                                  due=due or "Unknown", extra=bid_no))
    except Exception as e:
        print(f"[North Hempstead] Error: {e}")
    return out


# ─── 5. Town of Babylon (Bid Portal, Playwright) ──────────────────────────────

async def scrape_babylon(pw) -> list[dict]:
    out = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(user_agent=HEADERS["User-Agent"])
    page = await ctx.new_page()
    try:
        await page.goto("https://bidsapp.townofbabylon.com/Bid?statusId=2", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        # Babylon uses a DevExtreme list widget — wait for the bid links to appear
        try:
            await page.wait_for_selector("a[href*='/Bid/Bid/']", timeout=15000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)

        # Bid links are plain anchors: /Bid/Bid/NNN with title text
        for link in await page.query_selector_all("a[href*='/Bid/Bid/']"):
            title = (await link.inner_text()).strip()
            href = await link.get_attribute("href") or ""
            if not href.startswith("http"):
                href = "https://bidsapp.townofbabylon.com" + href
            if title and len(title) >= 5 and matches(title):
                out.append(result(SOURCE, "Town of Babylon", title, href))
    except PWTimeout:
        print("[Babylon] Timed out waiting for bid app")
    except Exception as e:
        print(f"[Babylon] Error: {e}")
    finally:
        await browser.close()
    return out


# ─── 6. Town of Southampton (ProcureWare, Playwright) ─────────────────────────
# ProcureWare is a JS single-page app; the public bid list renders client-side.
# Selectors here are a first pass — if this returns 0 with bids visibly open on
# the site, run this file with DIAG=1 to dump what Playwright sees:
#   set DIAG=1 && py rfx_suffolk_towns.py      (Windows cmd)

async def scrape_southampton(pw, diag: bool = False) -> list[dict]:
    out = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(user_agent=HEADERS["User-Agent"])
    page = await ctx.new_page()
    try:
        # NOTE: do NOT wait for networkidle here — ProcureWare polls constantly
        # and never goes idle, which made the old version time out every run.
        await page.goto("https://southampton.procureware.com/bids",
                        timeout=45000, wait_until="domcontentloaded")
        # Wait for the bid table if it shows up; proceed either way.
        try:
            await page.wait_for_selector("table tbody tr", timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(4000)  # SPA — give the XHR data time to render

        # ProcureWare typically renders bids as table rows with a detail link.
        rows = await page.query_selector_all("table tbody tr")
        if not rows:
            rows = await page.query_selector_all("tr")

        for row in rows:
            full_text = (await row.inner_text()).strip()
            if not full_text or len(full_text) < 8:
                continue
            # Skip closed/awarded bids — the table lists bid history, not just
            # open opportunities, and a bid's DEPARTMENT column (e.g. "Highway
            # Department") was matching keywords even for unrelated bulkhead/
            # building jobs. So: match on the TITLE cell only, and skip if the
            # row shows a closed/awarded status anywhere.
            if re.search(r"awarded|closed to bidding|cancel(?:l)?ed", full_text, re.I):
                continue

            link_el = await row.query_selector("a")
            href = (await link_el.get_attribute("href")) if link_el else ""
            if href and not href.startswith("http"):
                href = "https://southampton.procureware.com" + href

            cells = await row.query_selector_all("td")
            # First cell is usually the bid number (the link); title is
            # typically the next non-empty cell.
            title = ""
            for cell in cells[1:4]:
                t = (await cell.inner_text()).strip()
                if t and not re.match(r"^BP\d", t):
                    title = t
                    break
            if not title:
                title = full_text.split("\n")[0][:160]

            if matches(title):
                out.append(result(SOURCE, "Town of Southampton", title,
                                  href or "https://southampton.procureware.com/bids"))

    except PWTimeout:
        print("[Southampton] Timed out waiting for ProcureWare")
    except Exception as e:
        print(f"[Southampton] Error: {e}")
    finally:
        # Diagnostic dump runs even if scraping above failed, so a timeout
        # still tells us what (if anything) rendered.
        if diag:
            try:
                n_tables = len(await page.query_selector_all("table"))
                n_rows = len(await page.query_selector_all("tr"))
                links = await page.query_selector_all("a[href]")
                body_len = len(await page.inner_text("body"))
                print(f"[Southampton DIAG] tables={n_tables} rows={n_rows} "
                      f"links={len(links)} body_chars={body_len}")
                count = 0
                for link in links:
                    t = (await link.inner_text()).strip().replace("\n", " ")
                    h = await link.get_attribute("href") or ""
                    if len(t) >= 8 and count < 30:
                        print(f"  [{count:02d}] {t[:80]} -> {h[:90]}")
                        count += 1
            except Exception as de:
                print(f"[Southampton DIAG] dump failed: {de}")
        await browser.close()
    return out


# ─── Runner ───────────────────────────────────────────────────────────────────

async def _fetch_playwright_towns(diag: bool = False) -> list[dict]:
    async with async_playwright() as pw:
        babylon, southampton, islip = await asyncio.gather(
            scrape_babylon(pw),
            scrape_southampton(pw, diag=diag),
            scrape_islip(pw, diag=diag),
        )
    return babylon + southampton + islip


def get_all_suffolk_town_results() -> list[dict]:
    """Call this from rfx_alert.py — returns combined results from all towns."""
    import os
    diag = bool(os.environ.get("DIAG"))
    static_results = (
        scrape_suffolk_county()
        + scrape_brookhaven()
        + scrape_huntington()
        + scrape_smithtown()
        + scrape_civicengage_all()
        + scrape_north_hempstead()
        + scrape_oyster_bay()
    )
    playwright_results = asyncio.run(_fetch_playwright_towns(diag=diag))
    all_results = static_results + playwright_results
    print(f"[Suffolk Towns] static (Brookhaven/Huntington/Smithtown/4x CivicEngage"
          f"+Hempstead/NorthHempstead/OysterBay): {len(static_results)} | "
          f"Playwright (Babylon/Southampton): {len(playwright_results)}")
    return all_results


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = get_all_suffolk_town_results()
    print(f"\nTotal keyword-matched results: {len(results)}\n")
    for r in results:
        print(f"[{r['agency']}] {r['title']}")
        print(f"  URL: {r['url']}")
        print()
