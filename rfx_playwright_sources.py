"""
rfx_playwright_sources.py
-------------------------
Headless-Chromium scrapers for the four JavaScript-heavy portals that block
plain requests/Selenium: PANYNJ Bonfire, NYC PASSPort, Suffolk County,
Nassau County.

Each scraper renders the page, waits for content, grabs candidate rows, and
keyword-filters them through rfx_common.matches().

IMPORTANT — these four sites change their markup periodically, so the CSS
selectors below are a STARTING POINT and will likely need tuning against live
output. Run this file standalone first:

    py rfx_playwright_sources.py

It prints a per-source count and any errors. Paste that back and the selectors
get fixed source-by-source. One source failing never kills the others
(asyncio.gather runs with return_exceptions=True).
"""

import re
import asyncio
import datetime as dt
from rfx_common import matches, result

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None


def _clean_bonfire(text):
    """Turn a Bonfire row blob into (short_title, due_date)."""
    cells = [c.strip() for c in re.split(r"\t", text) if c.strip()]
    due = "Unknown"
    for c in cells:                     # the date cell, e.g. "Dec 31st 2026, 2:00 PM EST"
        if re.search(r"[A-Z][a-z]{2}\s+\d{1,2}(?:st|nd|rd|th)?\s+\d{4}", c):
            due = re.sub(r",?\s*\d{1,2}:\d{2}.*$", "", c).strip()
            break
    desc = max(cells, key=len) if cells else text   # the long description cell
    desc = re.sub(r"(?i)^RFP\s*#?\s*\d+\s*-\s*", "", desc)
    desc = re.sub(r"(?i)\s*-\s*OPEN\s*$", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) > 130:
        desc = desc[:127] + "..."
    nm = re.search(r"RFP\s*#?\s*(\d+)", text)
    return (f"RFP {nm.group(1)}: {desc}" if nm else desc), due


def _clean_passport(text):
    """Turn a PASSPort row blob into (short_title, due_date)."""
    cells = [c.strip() for c in re.split(r"\t", text) if c.strip()]
    if cells and cells[0].lower() == "edit":
        cells = cells[1:]
    titleid = cells[0] if cells else text[:80]
    agency = ""
    for c in cells:
        if c.isupper() and len(c) > 8 and any(
                w in c for w in ("DEPARTMENT", "AUTHORITY", "OFFICE", "ADMINISTRATION")):
            agency = c
            break
    parsed = []
    for s in re.findall(r"\d{1,2}/\d{1,2}/\d{4}", text):
        try:
            parsed.append(dt.datetime.strptime(s, "%m/%d/%Y"))
        except ValueError:
            pass
    due = max(parsed).strftime("%m/%d/%Y") if parsed else "Unknown"
    title = f"{titleid} ({agency})" if agency else titleid
    if len(title) > 140:
        title = title[:137] + "..."
    return title, due


# Common launch args that reduce bot-detection on these portals.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
NAV_TIMEOUT = 45_000  # ms


async def _new_page(pw):
    browser = await pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
    ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900})
    page = await ctx.new_page()
    page.set_default_timeout(NAV_TIMEOUT)
    return browser, page


# ─── Bonfire (generic — works for any *.bonfirehub.com portal) ─────────────────
async def _scrape_bonfire(pw, portal_url, agency, source) -> list[dict]:
    out = []
    origin = "/".join(portal_url.split("/")[:3])  # e.g. https://panynj.bonfirehub.com
    browser, page = await _new_page(pw)
    try:
        await page.goto(portal_url, wait_until="networkidle")
        # Bonfire renders opportunities into a table; wait for any row.
        await page.wait_for_selector("table tbody tr", timeout=NAV_TIMEOUT)
        rows = await page.query_selector_all("table tbody tr")
        for row in rows:
            text = (await row.inner_text()).replace("\n", " ").strip()
            if not matches(text):
                continue
            link = await row.query_selector("a")
            href = await link.get_attribute("href") if link else ""
            if href and href.startswith("/"):
                href = origin + href
            title, due = _clean_bonfire(text)
            out.append(result(source, agency, title, href or portal_url, due=due))
    except Exception as e:
        print(f"[{source}] {type(e).__name__}: {e}")
    finally:
        await browser.close()
    return out


async def scrape_bonfire(pw) -> list[dict]:
    return await _scrape_bonfire(
        pw, "https://panynj.bonfirehub.com/portal/?tab=openOpportunities",
        "PANYNJ", "PANYNJ Bonfire")


async def scrape_suffolk_bonfire(pw) -> list[dict]:
    return await _scrape_bonfire(
        pw, "https://suffolkcountyny.bonfirehub.com/portal/?tab=openOpportunities",
        "Suffolk County", "Suffolk Bonfire")


# ─── NYC PASSPort (public solicitations) ──────────────────────────────────────
async def scrape_passport(pw) -> list[dict]:
    url = "https://passport.cityofnewyork.us/page.aspx/en/rfp/request_browse_public"
    out = []
    browser, page = await _new_page(pw)
    try:
        await page.goto(url, wait_until="networkidle")
        # Don't hard-wait on row visibility (PASSPort rows often aren't "visible"
        # to Playwright until a search is run); just grab what rendered.
        await page.wait_for_timeout(3000)
        rows = await page.query_selector_all("table tr")
        for row in rows:
            text = (await row.inner_text()).replace("\n", " ").strip()
            if not text or not matches(text):
                continue
            link = await row.query_selector("a")
            href = await link.get_attribute("href") if link else ""
            if href and href.startswith("/"):
                href = "https://passport.cityofnewyork.us" + href
            title, due = _clean_passport(text)
            out.append(result("NYC PASSPort", "NYC", title, href or url, due=due))
    except Exception as e:
        print(f"[PASSPort] {type(e).__name__}: {e}")
    finally:
        await browser.close()
    return out


# ─── Suffolk County (procurement / bids) ──────────────────────────────────────
async def scrape_suffolk(pw) -> list[dict]:
    # Suffolk posts bids/RFPs here; confirm the live URL if this changes.
    url = "https://www.suffolkcountyny.gov/Departments/Economic-Development-and-Planning/Purchasing/Bid-Opportunities"
    out = []
    browser, page = await _new_page(pw)
    try:
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_load_state("networkidle")
        # Resilient: scan all links on the rendered page.
        links = await page.query_selector_all("a")
        for a in links:
            text = (await a.inner_text()).strip().replace("\n", " ")
            if not text or len(text) < 8 or not matches(text):
                continue
            href = await a.get_attribute("href") or ""
            # Skip map pins, addresses, mailto, and off-site junk.
            if "google.com/maps" in href or href.startswith("mailto:"):
                continue
            if href.startswith("/"):
                href = "https://www.suffolkcountyny.gov" + href
            # Keep only links that point into the county site.
            if "suffolkcountyny.gov" not in href:
                continue
            out.append(result("Suffolk County", "Suffolk County", text, href))
    except Exception as e:
        print(f"[Suffolk] {type(e).__name__}: {e}")
    finally:
        await browser.close()
    return out


# ─── Nassau County (Oracle APEX bid portal) ───────────────────────────────────
async def scrape_nassau(pw) -> list[dict]:
    url = "https://www.nassaucountyny.gov/2127/Bid-Opportunities"
    out = []
    browser, page = await _new_page(pw)
    try:
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_load_state("networkidle")
        links = await page.query_selector_all("a")
        for a in links:
            text = (await a.inner_text()).strip()
            if not text or len(text) < 8 or not matches(text):
                continue
            href = await a.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://www.nassaucountyny.gov" + href
            out.append(result("Nassau County", "Nassau County", text, href or url))
    except Exception as e:
        print(f"[Nassau] {type(e).__name__}: {e}")
    finally:
        await browser.close()
    return out


# ─── Orchestration ────────────────────────────────────────────────────────────
async def fetch_all_playwright() -> list[dict]:
    if async_playwright is None:
        print("[Playwright] not installed — run: py -m pip install playwright "
              "&& py -m playwright install chromium")
        return []

    async with async_playwright() as pw:
        results = await asyncio.gather(
            scrape_bonfire(pw),
            scrape_suffolk_bonfire(pw),
            scrape_passport(pw),
            return_exceptions=True,
        )

    names = ["Bonfire", "Suffolk Bonfire", "PASSPort"]
    all_results = []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            print(f"[{name}] crashed: {type(res).__name__}: {res}")
        else:
            print(f"[{name}] {len(res)} matches")
            all_results.extend(res)
    return all_results


def get_playwright_results() -> list[dict]:
    """Synchronous wrapper called from rfx_alert.py."""
    return asyncio.run(fetch_all_playwright())


# ─── Standalone test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    rows = get_playwright_results()
    print(f"\nTotal keyword-matched results: {len(rows)}\n")
    for r in rows:
        print(f"[{r['source']}] {r['title'][:90]}")
        print(f"   {r['url']}\n")
