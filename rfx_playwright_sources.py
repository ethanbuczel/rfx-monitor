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


# ─── Bonfire tuning ───────────────────────────────────────────────────────────
# The Bonfire LIST view omits Department and Close Date, so (when enabled) each
# matched opportunity's detail page is loaded to pull Department, Close Date and
# Status. Costs ~1 page load per matched item (~1-2 min for PANYNJ). Set False
# to skip it entirely — the digest still gets every opportunity, just with
# "Due: Unknown" and no department shown.
BONFIRE_ENRICH = True
# Optional department allow-list. None = show every department (recommended:
# hard-filtering risks dropping relevant work filed under another department).
# To restrict later, set e.g. {"Professional Technical & Advisory Services"}.
BONFIRE_DEPARTMENTS = None

_BONFIRE_BOILERPLATE = [
    r"Request\s+for\s+Proposals?\s+for\s+(?:the\s+)?Performance\s+of\s+Expert\s+Professional\s+",
    r"Request\s+for\s+Proposals?\s+for\s+(?:the\s+)?Performance\s+of\s+",
    r"Request\s+for\s+Proposals?\s+for\s+",
    r"Invitation\s+for\s+Bids?\s+for\s+",
]


def _shorten_bonfire_title(title: str) -> str:
    """Trim PANYNJ's verbose RFP boilerplate while keeping the ref # and the
    real subject. Returns the original if trimming would empty it."""
    t = title
    for pat in _BONFIRE_BOILERPLATE:
        t = re.sub(pat, "", t, flags=re.I)
    # Drop trailing "as requested on a '...' basis during 20xx..." clauses.
    t = re.sub(r"\s+as\s+requested\s+on\s+an?\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[-–—]\s*Two\s*\(?\s*2\s*\)?\s*Years?\s+Requirements?\s+Contract.*$",
               "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -–—,")
    if len(t) > 110:
        t = t[:107].rstrip(" -–—,") + "..."
    return t or title


# Commodity / goods-procurement bids (supply, deliver, rent equipment) are not
# design/engineering work, so they're dropped (unless they also carry a design/
# construction scope word). Set BONFIRE_DROP_COMMODITY=False to keep them.
BONFIRE_DROP_COMMODITY = True
_COMMODITY_RE = re.compile(
    r"\b(supply\s+and\s+deliver|supply\s*&\s*deliver|furnish\s+and\s+deliver"
    r"|purchase\s+of|procurement\s+of)\b", re.I)
_NONCOMMODITY_RE = re.compile(
    r"\b(design|engineering\s+services|planning|inspection|study|construction"
    r"|consultant|professional\s+services|repair|rehabilitation)\b", re.I)


def _bonfire_is_commodity(title: str, dept: str = "") -> bool:
    """True for goods/equipment procurement (not design work). A design or
    construction scope word anywhere in the title overrides it."""
    if "commodit" in (dept or "").lower():
        return True
    return bool(_COMMODITY_RE.search(title)) and not _NONCOMMODITY_RE.search(title)


# ─── 1. PANYNJ Bonfire ────────────────────────────────────────────────────────

async def _scrape_bonfire_portal(pw, portal_url: str, base_url: str,
                                 source: str, diag_tag: str) -> list[dict]:
    """Generic Bonfire scraper. Bonfire portals (PANYNJ, Suffolk County, etc.)
    all share the same table layout: each row has a 'View Opportunity' link plus
    cells holding the title, ref#, and close date. Parse rows that contain an
    /opportunities/ link."""
    results = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = await ctx.new_page()
    try:
        await page.goto(portal_url, timeout=35000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(2500)

        # Tab-based portals (e.g. Suffolk's ?tab=openOpportunities) sometimes
        # render the list only after the "Open Opportunities" tab is clicked
        # and need extra time for the table to populate. Try clicking it, then
        # wait longer. Harmless on portals that don't have the tab.
        for tab_txt in ("Open Opportunities", "Open Opportunity",
                        "Opportunities"):
            try:
                tab = await page.query_selector(f"text=/{tab_txt}/i")
                if tab:
                    await tab.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:
                continue
        # Wait for the table to actually fill in (more than the near-empty
        # shell). Retry a few times giving it time to render row content.
        for _ in range(4):
            rows_now = await page.query_selector_all("table tr")
            filled = False
            for rr in rows_now:
                if len((await rr.inner_text()).strip()) > 40:
                    filled = True
                    break
            if filled:
                break
            await page.wait_for_timeout(2500)
        for sel in ("table tr", "a[href*='/opportunities/']",
                    "div[class*='opportunity']", "[role='row']"):
            try:
                await page.wait_for_selector(sel, timeout=5000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(1500)

        import os as _os
        if _os.environ.get("DIAG"):
            body = await page.inner_text("body")
            print(f"[{diag_tag} DIAG] body_chars={len(body)}")
            for sel in ("table", "table tr", "a[href*='/opportunities/']",
                        "div[class*='opportunity']", "[role='row']"):
                els = await page.query_selector_all(sel)
                if els:
                    print(f"[{diag_tag} DIAG] {sel!r}: {len(els)}")
            links = await page.query_selector_all("a[href*='opportunit']")
            print(f"[{diag_tag} DIAG] opportunity links: {len(links)}")
            for lk in links[:12]:
                t = (await lk.inner_text()).strip().replace("\n", " ")
                h = await lk.get_attribute("href") or ""
                if len(t) >= 3:
                    print(f"    {t[:70]!r} -> {h[:70]}")

        # Prefer the SHARED pipeline matcher so Bonfire filters identically to
        # every other source. The old local KEYWORDS list silently diverged —
        # it lacked "engineering services", "APS", the signal phrases, etc. —
        # and quietly dropped relevant PANYNJ/Nassau work for a primary client.
        try:
            from rfx_common import matches as _match
        except Exception:
            _match = keyword_match

        # Bonfire's table lazily lays out row text: off-screen rows keep their
        # /opportunities/ link in the DOM but report EMPTY inner_text(), so a
        # single read only ever saw the ~dozen on-screen rows (a different dozen
        # each run — which is why the same open RFP kept vanishing between runs).
        # Fix: (a) read text_content() instead of inner_text() — it returns the
        # text even when a row isn't laid out; (b) scroll through the whole list
        # re-harvesting, keyed by href so each opportunity is captured once.
        harvested = {}   # href -> (full_text, cell_texts)

        async def _harvest():
            for row in await page.query_selector_all("table tr"):
                link = await row.query_selector("a[href*='/opportunities/']")
                if not link:
                    continue
                href = await link.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = base_url + href
                texts = []
                for c in await row.query_selector_all("td"):
                    t = re.sub(r"\s+", " ", (await c.text_content()) or "").strip()
                    if t:
                        texts.append(t)
                full = " ".join(texts)
                if len(full) < 15:
                    full = re.sub(r"\s+", " ",
                                  (await row.text_content()) or "").strip()
                if len(full) < 15:
                    continue
                # Keep the richest text seen for this opportunity.
                if href not in harvested or len(full) > len(harvested[href][0]):
                    harvested[href] = (full, texts)

        n_anchors = len(await page.query_selector_all("a[href*='/opportunities/']"))
        last, stable = -1, 0
        for _ in range(40):
            await _harvest()
            if n_anchors and len(harvested) >= n_anchors:
                break
            # Nudge the next chunk of rows into layout.
            try:
                tr = await page.query_selector_all("table tr")
                if tr:
                    await tr[-1].scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            try:
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            except Exception:
                pass
            await page.wait_for_timeout(500)
            n_anchors = max(n_anchors, len(
                await page.query_selector_all("a[href*='/opportunities/']")))
            if len(harvested) == last:
                stable += 1
                if stable >= 4:
                    break
            else:
                stable, last = 0, len(harvested)

        for href, (full, texts) in harvested.items():
            if not _match(full):
                continue
            cand = [t for t in texts
                    if t.lower() not in ("view opportunity", "view", "open")]
            raw_title = max(cand, key=len) if cand else full
            raw_title = re.sub(r"\bview opportunity\b", "", raw_title,
                               flags=re.I).strip()
            title = _shorten_bonfire_title(raw_title)
            # Drop commodity/goods bids up front (also saves a detail fetch).
            if BONFIRE_DROP_COMMODITY and _bonfire_is_commodity(raw_title):
                continue
            # List view rarely carries a parseable date; detail enrichment below
            # fills it in. Still try here in case a date is present (ordinals ok).
            date = ""
            dm = re.search(
                r"(\d{1,2}/\d{1,2}/\d{2,4}"
                r"|[A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", full)
            if dm:
                date = re.sub(r"(\d{1,2})(?:st|nd|rd|th)", r"\1", dm.group(1))
            if title and len(title) > 4:
                results.append({
                    "title": title[:200],
                    "agency": source,
                    "date": date,
                    "due": date or "Unknown",
                    "extra": "",
                    "url": href or portal_url,
                })

        # ── Detail enrichment: pull Department / Close Date / Status from each
        #    matched opportunity's detail page (the list view omits them).
        #    Fail-safe: any error leaves the item as-is. Drops an item only on a
        #    confidently-parsed past close date, an explicit closed status, or a
        #    department excluded by BONFIRE_DEPARTMENTS.
        if BONFIRE_ENRICH and results:
            import datetime as _dt
            today = _dt.date.today()
            _dumped = False
            kept = []
            for it in results:
                dept = status = close_raw = ""
                try:
                    await page.goto(it["url"], timeout=20000,
                                    wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector("text=/Close Date/i",
                                                     timeout=8000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(300)
                    body = re.sub(r"[ \t]+", " ",
                                  (await page.inner_text("body")) or "")

                    # One-time raw dump so we can see the exact label format the
                    # detail page uses (Department came back empty last run).
                    if _os.environ.get("DIAG") and not _dumped:
                        _dumped = True
                        snap = re.sub(r"\n{2,}", "\n", body)[:700]
                        print(f"[{diag_tag} DETAIL DUMP] {it['url']}\n{snap!r}")

                    # Clip a captured value at the next field label, so a value
                    # stays clean whether the detail page renders label/value on
                    # one line or separate lines.
                    def _clip(v):
                        return re.split(
                            r"\s*(?:Type|Status|Open Date|Close Date|Closing Date"
                            r"|Intent to Bid|Days Left|Ref\.?\s*#|Contact"
                            r"|Commodity|Project)\s*:",
                            v, maxsplit=1)[0].strip()

                    # Colon optional — some labels render the colon via CSS, so
                    # inner_text may read "Department\nProfessional Technical…".
                    m = re.search(r"Department\b\s*:?\s*([^\n]+)", body)
                    if m:
                        dept = _clip(m.group(1))[:70]
                    m = re.search(r"Status\b\s*:?\s*([A-Za-z]+)", body)
                    if m:
                        status = m.group(1).strip().lower()
                    m = (re.search(r"Close\s+Date\b\s*:?\s*([^\n]+)", body)
                         or re.search(r"Closing\s+Date\b\s*:?\s*([^\n]+)", body))
                    if m:
                        close_raw = _clip(m.group(1))
                except Exception:
                    pass

                # Commodity by department (e.g. "Commodity/Services") — drop.
                if BONFIRE_DROP_COMMODITY and _bonfire_is_commodity(
                        it["title"], dept):
                    continue

                due_date = None
                if close_raw:
                    cr = re.sub(r"(\d{1,2})(?:st|nd|rd|th)", r"\1", close_raw)
                    dm = (re.search(r"[A-Z][a-z]{2,8}\s+\d{1,2},?\s+\d{4}", cr)
                          or re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", cr))
                    if dm:
                        it["due"] = dm.group(0)
                        for fmt in ("%b %d %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d/%y"):
                            try:
                                due_date = _dt.datetime.strptime(
                                    re.sub(r",", "", dm.group(0)), fmt).date()
                                break
                            except ValueError:
                                continue
                if dept:
                    it["extra"] = dept
                    it["department"] = dept

                # Optional department allow-list (only applies when parsed).
                if (BONFIRE_DEPARTMENTS is not None and dept
                        and dept not in BONFIRE_DEPARTMENTS):
                    continue
                if status in ("closed", "archived", "awarded", "cancelled",
                              "canceled", "expired"):
                    continue
                if due_date is not None and due_date < today:
                    continue
                kept.append(it)
            results[:] = kept

        if _os.environ.get("DIAG"):
            n_due = sum(1 for r in results
                        if r.get("due", "Unknown") not in ("", "Unknown"))
            n_dept = sum(1 for r in results if r.get("department"))
            print(f"[{diag_tag} DIAG] anchors={n_anchors} "
                  f"rows_with_text={len(harvested)} matched={len(results)} "
                  f"with_due={n_due} with_dept={n_dept}")
            for r in results[:10]:
                oid = r["url"].rstrip("/").split("/")[-1]
                print(f"    {oid} | due={r.get('due')} "
                      f"| dept={r.get('department', '')[:32]!r} "
                      f"| {r['title'][:70]!r}")
    except PWTimeout:
        print(f"[{diag_tag}] Timed out waiting for content")
    except Exception as e:
        print(f"[{diag_tag}] Error: {e}")
    finally:
        await browser.close()
    return results


async def scrape_bonfire(pw) -> list[dict]:
    """PANYNJ (Port Authority) Bonfire portal."""
    return await _scrape_bonfire_portal(
        pw,
        portal_url="https://panynj.bonfirehub.com/opportunities",
        base_url="https://panynj.bonfirehub.com",
        source="PANYNJ Bonfire",
        diag_tag="Bonfire")


async def scrape_suffolk_bonfire(pw) -> list[dict]:
    """Suffolk County's own Bonfire portal — separate from BidNet. Tagged as its
    own source so we can see in the digest whether it surfaces professional-
    services RFPs that BidNet misses. Dedup collapses any true overlap."""
    return await _scrape_bonfire_portal(
        pw,
        portal_url="https://suffolkcountyny.bonfirehub.com/portal/?tab=openOpportunities",
        base_url="https://suffolkcountyny.bonfirehub.com",
        source="Suffolk County Bonfire",
        diag_tag="SuffolkBonfire")


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

# ─── 4. Nassau County ─────────────────────────────────────────────────────────

async def scrape_nassau(pw) -> list[dict]:
    results = []
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    page = await ctx.new_page()
    # Use the shared pipeline matcher, not the narrow local KEYWORDS list.
    try:
        from rfx_common import matches as _match
    except Exception:
        _match = keyword_match
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
            if not _match(full):
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
        bonfire, passport, nassau, suffolk_bf = await asyncio.gather(
            scrape_bonfire(pw),
            scrape_passport(pw),
            scrape_nassau(pw),
            scrape_suffolk_bonfire(pw),
        )
    all_results = bonfire + passport + nassau + suffolk_bf
    print(f"[Playwright] Bonfire: {len(bonfire)} | PASSPort: {len(passport)} | "
          f"Nassau: {len(nassau)} | SuffolkBonfire: {len(suffolk_bf)}")
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
        "Suffolk County Bonfire": "Suffolk County Bonfire",
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
