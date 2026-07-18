"""
rfx_playwright_sources.py
Playwright-based scrapers for JS-heavy RFx portals.
Replaces the Selenium/requests versions of these 4 sources:
  - PANYNJ Bonfire
  - NYC PASSPort
  - Suffolk County DPW
  - Nassau County

SETUP:
  pip install playwright --break-system-packages
  python -m playwright install chromium

Usage: import and call each function, or run this file standalone to test.
Each function returns a list of dicts:
  { "title": str, "agency": str, "date": str, "url": str }
"""

import asyncio
import re
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

KEYWORDS = [
    "traffic", "signal", "roadway", "intersection", "pavement",
    "transportation", "highway", "civil engineering", "ITS",
    "pedestrian", "bicycle", "signing", "striping", "marking",
    "bridge", "drainage", "corridor", "complete streets", "work zone",
    "MOT", "maintenance of traffic", "construction inspection"
]

def keyword_match(text: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in KEYWORDS)


# ─── 1. PANYNJ Bonfire ────────────────────────────────────────────────────────

async def scrape_bonfire(pw) -> list[dict]:
    results = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = await ctx.new_page()
    try:
        await page.goto("https://panynj.bonfirehub.com/opportunities", timeout=30000)
        # Wait for opportunity cards to load
        await page.wait_for_selector("div.opportunity-card, div.opportunity-list-item, article", timeout=20000)
        await page.wait_for_timeout(2000)  # let any lazy-load settle

        # Try multiple possible card selectors Bonfire uses
        cards = await page.query_selector_all("div.opportunity-card")
        if not cards:
            cards = await page.query_selector_all("div[class*='opportunity']")

        for card in cards:
            title_el = await card.query_selector("h2, h3, .title, [class*='title']")
            title = (await title_el.inner_text()).strip() if title_el else ""
            link_el = await card.query_selector("a")
            href = await link_el.get_attribute("href") if link_el else ""
            if href and not href.startswith("http"):
                href = "https://panynj.bonfirehub.com" + href
            date_el = await card.query_selector("[class*='date'], [class*='close'], time")
            date = (await date_el.inner_text()).strip() if date_el else ""

            if title and keyword_match(title):
                results.append({
                    "title": title,
                    "agency": "PANYNJ Bonfire",
                    "date": date,
                    "url": href or "https://panynj.bonfirehub.com/opportunities"
                })

        # Fallback: grab all text links if card parsing got nothing
        if not results:
            links = await page.query_selector_all("a[href*='/opportunities/']")
            for link in links:
                text = (await link.inner_text()).strip()
                href = await link.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = "https://panynj.bonfirehub.com" + href
                if text and keyword_match(text):
                    results.append({
                        "title": text,
                        "agency": "PANYNJ Bonfire",
                        "date": "",
                        "url": href
                    })

    except PWTimeout:
        print("[Bonfire] Timed out waiting for content")
    except Exception as e:
        print(f"[Bonfire] Error: {e}")
    finally:
        await browser.close()
    return results


# ─── 2. NYC PASSPort ──────────────────────────────────────────────────────────

async def scrape_passport(pw) -> list[dict]:
    results = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = await ctx.new_page()

    try:
        from rfx_common import matches as _match
    except Exception:
        _match = keyword_match

    async def _harvest_current_page():
        """Parse all keyword-matching solicitations on the loaded page.
        PASSPort renders each solicitation as a link to
        /bpm/process_manage_extranet/NNN inside a container div (NOT a table),
        so target those links and read their surrounding container text."""
        found = 0
        links = await page.query_selector_all(
            "a[href*='process_manage_extranet'], a[href*='/bpm/']")
        for lk in links:
            href = await lk.get_attribute("href") or ""
            if "process_manage_extranet" not in href and "/bpm/" not in href:
                continue
            # Full row text from the nearest container that holds the details.
            try:
                container = await lk.evaluate(
                    "el => { let p = el; for (let i=0;i<5;i++){ if(!p.parentElement) break; p = p.parentElement; if (p.innerText && p.innerText.length > 60) return p.innerText; } return el.innerText; }")
            except Exception:
                container = (await lk.inner_text())
            full = re.sub(r"\s+", " ", container or "").strip()
            if not _match(full):
                continue
            if not href.startswith("http"):
                href = "https://passport.cityofnewyork.us" + href
            if href in _seen_keys:
                continue
            _seen_keys.add(href)
            # Title: strip the leading "Edit " and use up to the first agency-ish
            # ALLCAPS run or a reasonable length.
            link_txt = re.sub(r"^\s*Edit\s+", "", (await lk.inner_text()).strip())
            title = link_txt.split("\n")[0][:160] or full[:120]
            # Date: grab a m/d/y if present in the row.
            dm = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", full)
            results.append({
                "title": title,
                "agency": "NYC PASSPort",
                "date": dm.group(1) if dm else "",
                "url": href,
            })
            found += 1
        return found

    _seen_keys = set()
    try:
        await page.goto(
            "https://passport.cityofnewyork.us/page.aspx/en/rfp/request_browse_public",
            timeout=30000
        )
        await page.wait_for_load_state("networkidle", timeout=25000)
        await page.wait_for_timeout(2000)

        import os as _os
        if _os.environ.get("DIAG"):
            # The results render as divs (iv-browse / iv-page), not a <table>.
            # Dump candidate result containers and their text so we can target
            # the real solicitation rows.
            for sel in (".iv-browse", ".iv-page", "[class*='iv-row']",
                        "[class*='result']", "[class*='row']",
                        "[class*='list-item']", "[class*='listItem']",
                        "[role='row']"):
                els = await page.query_selector_all(sel)
                if els:
                    print(f"[PASSPort DIAG] sel {sel!r}: {len(els)} elements")
            # Find elements that link to a solicitation detail page — those
            # anchor the real rows.
            links = await page.query_selector_all(
                "a[href*='process_manage_extranet'], a[href*='bpm']")
            print(f"[PASSPort DIAG] solicitation links found: {len(links)}")
            for i, lk in enumerate(links[:8]):
                txt = (await lk.inner_text()).strip().replace("\n", " ")
                href = await lk.get_attribute("href") or ""
                # Walk up to the container that holds this row's full text.
                container_txt = ""
                try:
                    container_txt = await lk.evaluate(
                        "el => { let p = el; for (let i=0;i<5;i++){ if(!p.parentElement) break; p = p.parentElement; if (p.innerText && p.innerText.length > 60) return p.innerText; } return el.innerText; }")
                except Exception:
                    pass
                container_txt = re.sub(r"\s+", " ", container_txt)[:180]
                print(f"[PASSPort DIAG] link[{i}] text={txt[:50]!r}")
                print(f"      href={href[:70]}")
                print(f"      container={container_txt!r}")

        # Page through all results. The board uses a pager; click the "next"
        # control until it's gone/disabled. Safety cap prevents infinite loops.
        MAX_PAGES = 25
        for _ in range(MAX_PAGES):
            await _harvest_current_page()
            # Find an enabled "next page" control. PASSPort's real pager (per
            # DIAG) is an aria-label='Next' button with class 'ui button icon';
            # it disables via a 'disabled' class/attr when on the last page.
            nxt = None
            for sel in ("[aria-label*='Next']:not([disabled])",
                        "button[aria-label*='Next']",
                        "a[aria-label*='Next']",
                        ".iv-page [aria-label*='Next']"):
                cand = await page.query_selector(sel)
                if cand:
                    cls = (await cand.get_attribute("class")) or ""
                    disabled_attr = await cand.get_attribute("disabled")
                    aria_dis = await cand.get_attribute("aria-disabled")
                    if ("disabled" in cls.lower() or disabled_attr is not None
                            or aria_dis == "true"):
                        continue
                    nxt = cand
                    break
            if not nxt:
                break
            try:
                await nxt.click()
                await page.wait_for_timeout(2500)  # let the next page render
            except Exception:
                break

    except PWTimeout:
        print("[PASSPort] Timed out — site may be slow")
    except Exception as e:
        print(f"[PASSPort] Error: {e}")
    finally:
        await browser.close()
    print(f"[PASSPort] {len(results)} matches")
    return results


# ─── 3. Suffolk County DPW ────────────────────────────────────────────────────

async def scrape_suffolk(pw) -> list[dict]:
    results = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = await ctx.new_page()
    try:
        await page.goto(
            "https://www.suffolkcountyny.gov/Departments/Public-Works/Procurement",
            timeout=30000
        )
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(1500)

        # Suffolk usually lists bids as links in a content area
        links = await page.query_selector_all("div.field-items a, div.content a, main a")
        for link in links:
            text = (await link.inner_text()).strip()
            href = await link.get_attribute("href") or ""
            if not href.startswith("http"):
                href = "https://www.suffolkcountyny.gov" + href
            # Skip nav/utility links
            if len(text) < 10 or not any(c.isalpha() for c in text):
                continue
            if keyword_match(text):
                results.append({
                    "title": text,
                    "agency": "Suffolk County DPW",
                    "date": "",
                    "url": href
                })

        # Also check for embedded BidNet iframe or redirect notice
        if not results:
            body = await page.inner_text("body")
            if "bidnet" in body.lower():
                results.append({
                    "title": "Suffolk County bids now on BidNet Direct",
                    "agency": "Suffolk County DPW",
                    "date": "",
                    "url": "https://www.bidnetdirect.com/suffolk-county-new-york"
                })

    except PWTimeout:
        print("[Suffolk] Timed out")
    except Exception as e:
        print(f"[Suffolk] Error: {e}")
    finally:
        await browser.close()
    return results


# ─── 4. Nassau County ─────────────────────────────────────────────────────────

async def scrape_nassau(pw) -> list[dict]:
    results = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = await ctx.new_page()
    try:
        # Nassau County formal solicitations — Oracle APEX app
        await page.goto(
            "https://apps.nassaucountyny.gov/f?p=172:1",
            timeout=30000
        )
        await page.wait_for_load_state("networkidle", timeout=25000)
        await page.wait_for_timeout(3000)  # Oracle APEX needs extra time

        # APEX renders rows as t-Report rows
        rows = await page.query_selector_all("tr.highlight-row, table.t-Report-report tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if not cells:
                continue
            texts = [(await c.inner_text()).strip() for c in cells]
            full = " ".join(texts)
            if not keyword_match(full):
                continue
            link_el = await row.query_selector("a")
            href = await link_el.get_attribute("href") if link_el else ""
            if href and not href.startswith("http"):
                href = "https://apps.nassaucountyny.gov" + href
            results.append({
                "title": texts[0] if texts else full[:120],
                "agency": "Nassau County",
                "date": texts[1] if len(texts) > 1 else "",
                "url": href or "https://apps.nassaucountyny.gov/f?p=172:1"
            })

    except PWTimeout:
        print("[Nassau] Timed out — Oracle APEX was slow")
    except Exception as e:
        print(f"[Nassau] Error: {e}")
    finally:
        await browser.close()
    return results


# ─── Runner ───────────────────────────────────────────────────────────────────

async def fetch_all_playwright() -> list[dict]:
    """Call all 4 Playwright scrapers and return combined results."""
    async with async_playwright() as pw:
        bonfire, passport, suffolk, nassau = await asyncio.gather(
            scrape_bonfire(pw),
            scrape_passport(pw),
            scrape_suffolk(pw),
            scrape_nassau(pw),
        )
    all_results = bonfire + passport + suffolk + nassau
    print(f"[Playwright] Bonfire: {len(bonfire)} | PASSPort: {len(passport)} | "
          f"Suffolk: {len(suffolk)} | Nassau: {len(nassau)}")
    return all_results


def get_playwright_results() -> list[dict]:
    """Synchronous wrapper — call this from your main rfx_alert.py."""
    return asyncio.run(fetch_all_playwright())


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = get_playwright_results()
    print(f"\nTotal keyword-matched results: {len(results)}\n")
    for r in results:
        print(f"[{r['agency']}] {r['title']}")
        print(f"  Date: {r['date'] or 'N/A'}")
        print(f"  URL:  {r['url']}")
        print()
