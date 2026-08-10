"""
rfx_nyc_portals.py
Extra sources beyond CROL:
  - NYC EDC (Economic Development Corporation)     — edc.nyc/rfps
  - SUNY (JAGGAER / SciQuest public bid site)      — incl. Stony Brook (LI)

SUNY is the big win here: it surfaces Stony Brook (Long Island) design/
engineering/ADA/drainage work that CROL never sees, because SUNY procures
through JAGGAER instead of the City Record. EDC's formal RFPs also appear in
CROL, so it's kept mainly to catch anything CROL misses; watch for overlap.

NYC DDC was tried and dropped — its DDC Anywhere portal loads projects via JS
that Playwright couldn't reach, and DDC construction already routes through
PASSPort (already scraped). scrape_ddc() remains below for reference, unused.

Both active scrapers are Playwright-based, fail gracefully, and honor DIAG=1.
"""

import asyncio
import os
import re

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from rfx_common import matches, result

HEADERS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

DDC_URL = "https://designbuild.ddcanywhere.nyc/?SearchString=&ProjectStatus=All-Active-PRJs"
EDC_URL = "https://edc.nyc/rfps"
SUNY_URL = ("https://bids.sciquest.com/apps/Router/PublicEvent"
            "?tab=PHX_NAV_SourcingAllOpps&CustomerOrg=SUNY")

SOURCE_DDC = "NYC DDC"
SOURCE_EDC = "NYC EDC"
SOURCE_SUNY = "SUNY (Stony Brook/Buffalo)"


async def _new_page(pw):
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(user_agent=HEADERS_UA)
    page = await ctx.new_page()
    return browser, page


# ─── NYC DDC — DDC Anywhere active procurements ───────────────────────────────
async def scrape_ddc(pw, diag: bool = False) -> list[dict]:
    out = []
    browser, page = await _new_page(pw)
    try:
        await page.goto(DDC_URL, timeout=35000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)  # JS renders the project list
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        # The project list loads via XHR after the shell. Wait for either a
        # project link or a known container to appear, then give it a moment.
        for sel in ("a[href*='project']", "a[href*='Project']",
                    "[class*='project']", "[class*='card']", "table tr"):
            try:
                await page.wait_for_selector(sel, timeout=6000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(2500)

        # DDC Anywhere renders projects as cards/rows. Harvest links to project
        # detail pages plus their surrounding text.
        anchors = await page.query_selector_all("a[href]")
        seen = set()
        for a in anchors:
            href = await a.get_attribute("href") or ""
            txt = (await a.inner_text()).strip()
            if len(txt) < 8:
                continue
            # Project detail links on this portal contain 'project' or an id.
            if not re.search(r"project|prj|solicit|rfp|rfq", href, re.I):
                continue
            # Pull surrounding container text for keyword matching + dates.
            try:
                container = await a.evaluate(
                    "el => { let p = el; for (let i=0;i<4;i++){ if(!p.parentElement) break; p = p.parentElement; if (p.innerText && p.innerText.length > 40) return p.innerText; } return el.innerText; }")
            except Exception:
                container = txt
            full = re.sub(r"\s+", " ", container or txt)
            if full[:80] in seen:
                continue
            seen.add(full[:80])
            if not href.startswith("http"):
                href = "https://designbuild.ddcanywhere.nyc" + (
                    href if href.startswith("/") else "/" + href)
            if matches(full):
                dm = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", full)
                out.append(result(SOURCE_DDC, "NYC DDC", txt[:150], href,
                                  due=dm.group(1) if dm else "Unknown"))

        if diag:
            n_links = len(anchors)
            body = await page.inner_text("body")
            print(f"[DDC DIAG] anchors={n_links} body_chars={len(body)} "
                  f"matched={len(out)}")
            for a in anchors[:15]:
                t = (await a.inner_text()).strip().replace("\n", " ")
                h = await a.get_attribute("href") or ""
                if len(t) >= 5:
                    print(f"    {t[:60]!r} -> {h[:70]}")
    except PWTimeout:
        print("[DDC] Timed out")
    except Exception as e:
        print(f"[DDC] Error: {e}")
    finally:
        await browser.close()
    print(f"[DDC] {len(out)} matches")
    return out


# ─── NYC EDC — edc.nyc/rfps ───────────────────────────────────────────────────
async def scrape_edc(pw, diag: bool = False) -> list[dict]:
    out = []
    browser, page = await _new_page(pw)
    try:
        await page.goto(EDC_URL, timeout=35000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        # EDC lists each RFP as a card linking to a project page (/rfp-name or
        # /request-...). Harvest those.
        anchors = await page.query_selector_all("a[href]")
        seen = set()
        for a in anchors:
            href = await a.get_attribute("href") or ""
            txt = (await a.inner_text()).strip()
            if len(txt) < 10:
                continue
            # EDC project/rfp pages: skip nav/utility links.
            low = href.lower()
            if any(w in low for w in ("mailto:", "twitter", "facebook",
                                      "linkedin", "/opportunity-mwdbe",
                                      "subscribe", "newsletter", "#")):
                continue
            if txt.lower() in seen:
                continue
            seen.add(txt.lower())
            if not href.startswith("http"):
                href = "https://edc.nyc" + (href if href.startswith("/")
                                            else "/" + href)
            # EDC RFP titles are descriptive; keyword-match the title.
            if matches(txt):
                out.append(result(SOURCE_EDC, "NYC EDC", txt[:160], href,
                                  due="Unknown"))

        if diag:
            body = await page.inner_text("body")
            print(f"[EDC DIAG] anchors={len(anchors)} body_chars={len(body)} "
                  f"matched={len(out)}")
            for a in anchors[:20]:
                t = (await a.inner_text()).strip().replace("\n", " ")
                h = await a.get_attribute("href") or ""
                if len(t) >= 8:
                    print(f"    {t[:60]!r} -> {h[:70]}")
    except PWTimeout:
        print("[EDC] Timed out")
    except Exception as e:
        print(f"[EDC] Error: {e}")
    finally:
        await browser.close()
    print(f"[EDC] {len(out)} matches")
    return out


# ─── SUNY — JAGGAER / SciQuest public bid site ────────────────────────────────
async def scrape_suny(pw, diag: bool = False) -> list[dict]:
    out = []
    browser, page = await _new_page(pw)
    try:
        await page.goto(SUNY_URL, timeout=35000, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass

        # The public bid site renders a table of open events. Rows carry the
        # solicitation title, org (campus), and dates.
        rows = await page.query_selector_all("tr")
        seen = set()
        for row in rows:
            text = (await row.inner_text()).strip()
            if len(text) < 12:
                continue
            full = re.sub(r"\s+", " ", text)
            # Only OPEN events (skip Closed/Canceled/Awarded rows).
            status = full.split(" ")[0].lower()
            if status in ("closed", "canceled", "cancelled", "awarded"):
                continue
            link = await row.query_selector("a[href]")
            href = (await link.get_attribute("href")) if link else ""
            if href and not href.startswith("http"):
                href = "https://bids.sciquest.com" + href
            if full[:80] in seen:
                continue
            seen.add(full[:80])
            # Title: strip leading "Open " status, and collapse the doubled
            # title JAGGAER renders (it repeats the name). Take text up to the
            # first "Open <date>" / "Close" / "Type" marker.
            body_txt = re.sub(r"^(Open|Closed|Canceled|Cancelled)\s+", "",
                              full, flags=re.I)
            title = re.split(r"\s+(?:Open|Close|Type|Method of Award)\s",
                             body_txt)[0]
            # De-duplicate the repeated half ("X X" -> "X").
            half = len(title) // 2
            if title[:half].strip() and title[:half].strip() == \
                    title[half:].strip():
                title = title[:half].strip()
            title = title.strip()[:150]
            # Skip obvious non-design noise (OGS vehicle buys, etc.).
            low = full.lower()
            if any(w in low for w in ("vehicle purchase", "ogs vehicle",
                                      "pick up truck", "police vehicle",
                                      "chevrolet")):
                continue
            if matches(full):
                dm = re.search(r"Close\s+(\d{1,2}/\d{1,2}/\d{2,4})", full)
                if not dm:
                    dm = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", full)
                out.append(result(SOURCE_SUNY, "SUNY", title,
                                  href or SUNY_URL,
                                  due=dm.group(1) if dm else "Unknown"))

        if diag:
            print(f"[SUNY DIAG] rows={len(rows)} matched={len(out)}")
            shown = 0
            for row in rows:
                t = (await row.inner_text()).strip().replace("\n", " ")
                if len(t) >= 10 and shown < 15:
                    print(f"    {t[:90]!r}")
                    shown += 1
    except PWTimeout:
        print("[SUNY] Timed out")
    except Exception as e:
        print(f"[SUNY] Error: {e}")
    finally:
        await browser.close()
    print(f"[SUNY] {len(out)} matches")
    return out


async def _guard(coro, name: str, limit: int):
    """Hard time cap per scraper so a hang can't freeze the whole job."""
    try:
        return await asyncio.wait_for(coro, timeout=limit)
    except asyncio.TimeoutError:
        print(f"[{name}] hard-timeout after {limit}s — skipping")
        return []
    except Exception as e:
        print(f"[{name}] error: {e}")
        return []


async def _fetch_all(diag: bool = False) -> list[dict]:
    # NOTE: NYC DDC was dropped — its DDC Anywhere portal loads the project
    # list via JS that Playwright couldn't reach (only the nav shell rendered),
    # AND DDC's construction solicitations already route through PASSPort, which
    # is scraped thoroughly. So it was duplicate data behind a stubborn wall.
    # scrape_ddc() is kept in the file for reference but no longer called.
    async with async_playwright() as pw:
        edc, suny = await asyncio.gather(
            _guard(scrape_edc(pw, diag=diag), "EDC", 75),
            _guard(scrape_suny(pw, diag=diag), "SUNY", 75),
        )
    return edc + suny


def get_nyc_portal_results() -> list[dict]:
    """Called from rfx_alert.py — DDC + EDC + SUNY combined."""
    diag = bool(os.environ.get("DIAG"))
    try:
        return asyncio.run(_fetch_all(diag=diag))
    except Exception as e:
        print(f"[NYC Portals] fatal error (continuing without): {e}")
        return []


if __name__ == "__main__":
    rows = get_nyc_portal_results()
    print(f"\nTotal NYC-portal matches: {len(rows)}\n")
    for r in rows:
        print(f"[{r['source']}] {r['title']}")
        print(f"   Due: {r.get('due','')}\n   {r['url']}\n")
