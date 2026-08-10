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
        await page.goto("https://panynj.bonfirehub.com/opportunities", timeout=35000)
        # Bonfire changed its markup before; don't HARD-fail on one selector.
        # Wait for network to settle, then probe several possible containers.
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)
        for sel in ("div.opportunity-card", "div[class*='opportunity']",
                    "table tr", "a[href*='/opportunities/']",
                    "[class*='card']", "[role='row']"):
            try:
                await page.wait_for_selector(sel, timeout=5000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(1500)

        import os as _os
        if _os.environ.get("DIAG"):
            body = await page.inner_text("body")
            print(f"[Bonfire DIAG] body_chars={len(body)}")
            for sel in ("div.opportunity-card", "div[class*='opportunity']",
                        "table", "table tr", "a[href*='/opportunities/']",
                        "[class*='card']", "[role='row']", "h2", "h3"):
                els = await page.query_selector_all(sel)
                if els:
                    print(f"[Bonfire DIAG] {sel!r}: {len(els)}")
            # Dump the first several opportunity-ish links
            links = await page.query_selector_all("a[href*='opportunit']")
            print(f"[Bonfire DIAG] opportunity links: {len(links)}")
            for lk in links[:12]:
                t = (await lk.inner_text()).strip().replace("\n", " ")
                h = await lk.get_attribute("href") or ""
                if len(t) >= 3:
                    print(f"    {t[:70]!r} -> {h[:70]}")

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

            # STATUS FILTER: PASSPort rows carry a status word. Only keep ones
            # that are actually open for bidding. Drop dead stages — the date
            # shown for those is a status-change timestamp (often long past),
            # which is why closed jobs were leaking in with stale dates.
            low = full.lower()
            DEAD = ("closed", "selections made", "responses received",
                    "response received", "cancelled", "canceled", "awarded",
                    "not awarded", "selection made", "expired")
            if any(d in low for d in DEAD):
                continue
            # Prefer explicitly-open statuses; if no recognizable status at all,
            # keep it (some rows omit status) so we don't over-filter.
            OPEN = ("released", "open", "accepting", "active", "rfi", "rfp",
                    "rfq", "invitation", "solicit")
            has_status = any(s in low for s in OPEN) or not any(
                w in low for w in ("released", "closed", "responses",
                                    "selections", "cancel", "award", "expired"))
            if not has_status:
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
            # Browse-page date is unreliable (often a status timestamp, and the
            # real Due Date lives only on the detail page). Store the candidate;
            # the real due date is resolved in a second pass below.
            results.append({
                "title": title,
                "agency": "NYC PASSPort",
                "date": "",
                "due": "Unknown",
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

        # ── Second pass: resolve each item's REAL Due Date from its detail
        # page and drop anything past due. The browse page doesn't expose the
        # due date (it's under "Key Dates > Due Date" on the detail page), which
        # is why expired items were slipping through as "Unknown". Slower (one
        # load per item) but accurate.
        import datetime as _dt
        _diag_detail = _os.environ.get("DIAG")
        today = _dt.date.today()
        kept = []
        for item in results:
            due_date = None
            try:
                await page.goto(item["url"], timeout=25000,
                                wait_until="domcontentloaded")
                # Key Dates render via JS after load — wait for network to
                # settle and for the "Due Date" label to actually appear.
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                try:
                    await page.wait_for_selector("text=/Due Date/i", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(800)

                # The dates live in INPUT field values (the body text only shows
                # the "(M/d/yyyy)" placeholder labels). Find the input whose
                # label/preceding text is "Due Date" and read its .value.
                due_str = await page.evaluate(r"""
                    () => {
                      const dateRe = /^\d{1,2}\/\d{1,2}\/\d{2,4}$/;
                      const inputs = Array.from(document.querySelectorAll('input'));
                      // 1) Prefer an input whose id/name/aria-label points to the
                      //    (non-questions, non-contract) Due Date.
                      for (const inp of inputs) {
                        const val = (inp.value || '').trim();
                        if (!dateRe.test(val)) continue;
                        const meta = ((inp.id||'') + ' ' + (inp.name||'') + ' ' +
                                      (inp.getAttribute('aria-label')||'')).toLowerCase();
                        if (meta.includes('due') && !meta.includes('question')
                            && !meta.includes('contract') && !meta.includes('start')
                            && !meta.includes('end')) {
                          return val;
                        }
                      }
                      // 2) Fallback: label text sitting before the input.
                      for (const inp of inputs) {
                        const val = (inp.value || '').trim();
                        if (!dateRe.test(val)) continue;
                        let label = '', el = inp;
                        for (let i = 0; i < 6 && el; i++) {
                          el = el.previousElementSibling || el.parentElement;
                          if (el && el.innerText) { label = el.innerText.trim().slice(0,60); if (label) break; }
                        }
                        const L = label.toLowerCase();
                        if (L.includes('due date') && !L.includes('questions')
                            && !L.includes('contract')) return val;
                      }
                      return '';
                    }
                """)
                m = re.match(r"(\d{1,2}/\d{1,2}/\d{2,4})", due_str or "")
                if _diag_detail:
                    print(f"[PASSPort DETAIL] {item['title'][:40]!r} "
                          f"due_input={due_str!r}")
                if m:
                    item["due"] = m.group(1)
                    item["date"] = m.group(1)
                    try:
                        mm, dd, yy = (int(x) for x in m.group(1).split("/"))
                        yy += 2000 if yy < 100 else 0
                        due_date = _dt.date(yy, mm, dd)
                    except ValueError:
                        due_date = None
            except Exception as e:
                if _diag_detail:
                    print(f"[PASSPort DETAIL] {item['title'][:40]!r} ERROR {e}")
            if due_date is not None and due_date < today:
                continue
            kept.append(item)
        results[:] = kept

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
    """Call the Playwright scrapers and return combined results.
    NOTE: Suffolk County is no longer scraped here — the old scrape_suffolk hit
    an informational DPW page and returned nav links ('Highway Maintenance',
    etc.), not bids. Suffolk County's real open bids are on BidNet and are now
    handled by scrape_suffolk_county() in rfx_suffolk_towns.py."""
    async with async_playwright() as pw:
        bonfire, passport, nassau = await asyncio.gather(
            scrape_bonfire(pw),
            scrape_passport(pw),
            scrape_nassau(pw),
        )
    all_results = bonfire + passport + nassau
    print(f"[Playwright] Bonfire: {len(bonfire)} | PASSPort: {len(passport)} | "
          f"Nassau: {len(nassau)}")
    return all_results


def get_playwright_results() -> list[dict]:
    """Synchronous wrapper — call this from your main rfx_alert.py."""
    rows = asyncio.run(fetch_all_playwright())
    # These scrapers build plain dicts without a 'source' key; the digest's
    # dedup/new-tracking (rfx_alert._seen_key) requires 'source' on every item.
    # Map agency -> section source so each lands in the right digest section.
    agency_to_source = {
        "PANYNJ Bonfire": "PANYNJ Bonfire",
        "NYC PASSPort": "NYC PASSPort",
        "Suffolk County DPW": "Suffolk County DPW",
        "Nassau County": "Nassau County",
    }
    for r in rows:
        r.setdefault("source", agency_to_source.get(r.get("agency", ""),
                                                     r.get("agency", "Other")))
        # Ensure the other keys the digest expects always exist.
        r.setdefault("due", r.get("due", "Unknown"))
        r.setdefault("extra", r.get("extra", ""))
    return rows


# ─── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = get_playwright_results()
    print(f"\nTotal keyword-matched results: {len(results)}\n")
    for r in results:
        print(f"[{r['agency']}] {r['title']}")
        print(f"  Date: {r['date'] or 'N/A'}")
        print(f"  URL:  {r['url']}")
        print()
