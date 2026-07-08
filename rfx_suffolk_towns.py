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
  - Town of Islip — requires account login to view bid list at all

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
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from rfx_common import matches, result

SOURCE = "Suffolk Towns"   # one digest section for all towns; agency = the town

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


# ─── 1. Town of Brookhaven (BidNet Direct, static) ────────────────────────────

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
                out.append(result(SOURCE, "Town of Brookhaven", title, href,
                                  extra=row_text[:120]))
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
    ("Town of Riverhead",      "https://www.townofriverheadny.gov/bids.aspx"),
    ("Town of East Hampton",   "https://ehamptonny.gov/Bids.aspx"),
    ("Town of Southold",       "https://www.southoldtownny.gov/Bids.aspx"),
    ("Town of Shelter Island", "https://www.shelterislandtown.gov/Bids.aspx"),
]


def scrape_civicengage_town(town_name: str, url: str) -> list[dict]:
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
                out.append(result(SOURCE, town_name, title, href))
    except Exception as e:
        print(f"[{town_name}] Error: {e}")
    return out


def scrape_civicengage_all() -> list[dict]:
    out = []
    for name, url in CIVICENGAGE_TOWNS:
        out += scrape_civicengage_town(name, url)
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
        await page.goto("https://southampton.procureware.com/bids", timeout=35000)
        await page.wait_for_load_state("networkidle", timeout=25000)
        await page.wait_for_timeout(3000)  # SPA — give the XHR data time to render

        # ProcureWare typically renders bids as table rows with a detail link.
        rows = await page.query_selector_all("table tbody tr")
        if not rows:
            rows = await page.query_selector_all("tr")

        for row in rows:
            text = (await row.inner_text()).strip()
            if not text or len(text) < 8:
                continue
            link_el = await row.query_selector("a")
            href = (await link_el.get_attribute("href")) if link_el else ""
            if href and not href.startswith("http"):
                href = "https://southampton.procureware.com" + href
            if matches(text):
                title = text.split("\n")[0][:160]
                out.append(result(SOURCE, "Town of Southampton", title,
                                  href or "https://southampton.procureware.com/bids"))

        if diag:
            n_tables = len(await page.query_selector_all("table"))
            n_rows = len(await page.query_selector_all("tr"))
            links = await page.query_selector_all("a[href]")
            print(f"[Southampton DIAG] tables={n_tables} rows={n_rows} links={len(links)}")
            count = 0
            for link in links:
                t = (await link.inner_text()).strip().replace("\n", " ")
                h = await link.get_attribute("href") or ""
                if len(t) >= 8 and count < 30:
                    print(f"  [{count:02d}] {t[:80]} -> {h[:90]}")
                    count += 1

    except PWTimeout:
        print("[Southampton] Timed out waiting for ProcureWare")
    except Exception as e:
        print(f"[Southampton] Error: {e}")
    finally:
        await browser.close()
    return out


# ─── Runner ───────────────────────────────────────────────────────────────────

async def _fetch_playwright_towns(diag: bool = False) -> list[dict]:
    async with async_playwright() as pw:
        babylon, southampton = await asyncio.gather(
            scrape_babylon(pw),
            scrape_southampton(pw, diag=diag),
        )
    return babylon + southampton


def get_all_suffolk_town_results() -> list[dict]:
    """Call this from rfx_alert.py — returns combined results from all towns."""
    import os
    diag = bool(os.environ.get("DIAG"))
    static_results = (
        scrape_brookhaven()
        + scrape_huntington()
        + scrape_smithtown()
        + scrape_civicengage_all()
    )
    playwright_results = asyncio.run(_fetch_playwright_towns(diag=diag))
    all_results = static_results + playwright_results
    print(f"[Suffolk Towns] static (Brookhaven/Huntington/Smithtown/Riverhead/"
          f"EastHampton/Southold/ShelterIsland): {len(static_results)} | "
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
