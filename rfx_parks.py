"""
rfx_parks.py — NYS Office of Parks, Recreation & Historic Preservation (OPRHP).

WHY THIS EXISTS
---------------
NYS Parks posts two kinds of solicitations:
  • Capital / Construction (D00xxxx) and Concessions (X00xxxx) → cross-listed on
    the NYS Contract Reporter, which rfx_alert.fetch_nyscr() already covers.
  • Consultant / service contracts (Cxxxxxx) and equipment buys (OPxxxxx) → listed
    ONLY on the Parks procurement page, never on the Contract Reporter.

That second bucket is why in-region professional jobs like the Long Island
"Consultant for Piping Plover Habitat Conservation Plan" (C003761) were being
missed entirely — no source we scraped ever saw them.

DESIGN (per Ethan, 2026-11-24)
------------------------------
  • Let the AI classifier do the real judging — the keyword filter catches very
    little Parks work, so this scraper is intentionally PERMISSIVE: it surfaces
    every open solicitation EXCEPT the obvious commodity/operations junk (refuse,
    geese, fireworks, golf carts, pesticides, boats, fuel, …), then hands the rest
    to rfx_classify, which tags relevance AND flags "far from region" so upstate
    parks (Niagara, Letchworth, Green Lakes) get greyed/flagged, not treated like
    Long Island.
  • "consultant" is scoped HERE (not in the global keyword list) — a consultant
    job on Parks is worth surfacing; a consultant job on every other source is not.

FETCH: parks.ny.gov sits behind bot-protection that 403s plain `requests` (same
as Tarrytown), so we load the page in headless Chromium (Playwright) and parse the
RENDERED html. Row parser is defensive against the page's messy table and prints a
DIAG dump of every row — run once with DIAG=1 and check the log to confirm
titles/dues/links land right.

get_parks_results() -> list[dict]   # standard result dicts, for rfx_alert
"""

import os
import re
import asyncio
import datetime as dt

PARKS_URL = "https://www.parks.ny.gov/business/procurement-opportunities.aspx"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Commodity / operations junk — Parks lists a lot of it. These are deliberately
# specific so they can't clip real design/consultant/infrastructure work. A row
# whose text hits any of these is dropped before it ever reaches the classifier.
PARKS_NEGATIVE = [
    "refuse", "recycling", "recycle", "trash", "garbage", "waste removal",
    "geese", "goose", "wildlife control", "pest control", "pesticide",
    "herbicide", "fungicide", "insecticide", "fertilizer",
    "fireworks", "firework", "drone show", "laser show", "concert",
    "golf cart", "golf carts", "tram", "boom lift", "aerial lift",
    "spider lift", "scissor lift", "forklift", "mower", "mowing",
    "boat", "landing craft", "watercraft", "vessel", "bunk trailer",
    "fuel", "gasoline", "diesel", "propane", "heating oil",
    "uniform", "vending", "concession", "snack", "catering", "food service",
    "beverage", "tree removal", "removal of cut", "stump", "hay", "mulch",
    "topsoil", "seed", "plant material", "lifeguard", "security guard",
    "janitorial", "cleaning service", "laundry", "portable toilet",
    "restroom cleaning", "snow removal", "plowing", "conservation corp",
]

# Procurement number patterns seen on the page (C003834, OP38700, D00xxxx, X00xxx),
# optionally prefixed with IFB/RFP/RFQ/RFI.
_PNUM_RE = re.compile(
    r"\b(?:IFB|RFP|RFQ|RFI)?\s*-?\s*((?:C|D|X|OP|RF)\s?\d{4,7}[A-Z]?)\b", re.I)
_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")


def _is_junk(text: str) -> bool:
    low = text.lower()
    return any(neg in low for neg in PARKS_NEGATIVE)


def _parks_matches(text: str) -> bool:
    """Permissive: keep anything that isn't commodity/ops junk. The classifier,
    not keywords, decides real relevance (and far-from-region). This is where
    'consultant' is effectively scoped to Parks — a non-junk Parks row is kept
    regardless of whether it hits a traffic keyword."""
    if not text or _is_junk(text):
        return False
    return True


def _latest_date(text: str):
    """Return (display_str, date) for the LATEST m/d/y in the text — on a Parks
    row the bid-due date is the last/largest date (release < due)."""
    best = None
    best_str = ""
    for mm, dd, yy in _DATE_RE.findall(text):
        y = int(yy) + (2000 if len(yy) == 2 else 0)
        try:
            d = dt.date(y, int(mm), int(dd))
        except ValueError:
            continue
        if best is None or d > best:
            best, best_str = d, f"{int(mm):02d}/{int(dd):02d}/{y}"
    return best_str, best


def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    return "https://www.parks.ny.gov" + (href if href.startswith("/") else "/" + href)


async def _fetch_html() -> str:
    """Load the Parks procurement page in headless Chromium and return the
    RENDERED html. Plain requests gets a 403 here (bot-protection), so a real
    browser is required — same approach as the Tarrytown village scraper."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA)
        page = await ctx.new_page()
        try:
            await page.goto(PARKS_URL, timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                pass
            await page.wait_for_timeout(2500)  # let any challenge/table settle
            # If a Cloudflare interstitial is up, the real table won't be present
            # yet; give it one more beat.
            html = await page.content()
            if "procurement" not in html.lower() and "C00" not in html:
                await page.wait_for_timeout(4000)
                html = await page.content()
            return html
        finally:
            await browser.close()


def _parse_html(html: str, diag: bool) -> list[dict]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("[Parks] beautifulsoup4 not installed — skipping.")
        return []
    try:
        from rfx_common import result
    except Exception:
        def result(source, agency, title, url, date=None, extra=None, due=None):
            return {"source": source, "agency": agency, "title": title,
                    "url": url, "date": date or "", "due": due or "Unknown",
                    "extra": extra or ""}

    soup = BeautifulSoup(html, "html.parser")
    today = dt.date.today()
    out, seen = [], set()

    rows = soup.find_all("tr")
    if diag:
        print(f"[Parks DIAG] <tr> rows found: {len(rows)}")

    kept = 0
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row_text = re.sub(r"\s+", " ", tr.get_text(" ", strip=True)).strip()
        if not row_text:
            continue

        pm = _PNUM_RE.search(row_text)
        if not pm:
            continue  # not a solicitation row
        pnum = re.sub(r"\s+", "", pm.group(1)).upper()

        low = row_text.lower()
        if any(w in low for w in ("closed", "withdrawn", "cancelled",
                                  "canceled", "awarded", "rescinded")):
            if diag:
                print(f"[Parks DIAG] skip CLOSED {pnum}: {row_text[:80]!r}")
            continue

        # Title = the longest alphabetic cell that isn't the procurement number
        # or a short date cell (best-effort; DIAG shows the raw row so we can pin
        # it to a fixed column if the live table needs it).
        title = ""
        for c in cells:
            t = re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip()
            if not t or _PNUM_RE.fullmatch(t):
                continue
            if _DATE_RE.search(t) and len(t) < 40:
                continue
            if sum(ch.isalpha() for ch in t) > sum(ch.isalpha() for ch in title):
                title = t
        title = re.sub(r"^(?:IFB|RFP|RFQ|RFI)\s+", "", title).strip()
        if not title:
            title = row_text[:120]

        full_text = f"{pnum} {title}"
        if not _parks_matches(full_text):
            if diag:
                print(f"[Parks DIAG] drop junk  {pnum}: {title[:70]!r}")
            continue

        due_str, due_date = _latest_date(row_text)
        if due_date is not None and due_date < today:
            if diag:
                print(f"[Parks DIAG] skip PASTDUE {pnum} ({due_str}): {title[:60]!r}")
            continue

        url = PARKS_URL
        for a in tr.find_all("a", href=True):
            if ".pdf" in a["href"].lower():
                url = _abs(a["href"])
                break

        key = (pnum, title.lower())
        if key in seen:
            continue
        seen.add(key)

        if diag:
            print(f"[Parks DIAG] KEEP {pnum} due={due_str or 'Unknown'} "
                  f"| {title[:70]!r} | {url[:70]}")

        out.append(result("NYS Parks", "NYS Parks", title, url,
                           due=due_str or "Unknown", extra=pnum))
        kept += 1

    print(f"[Parks] {kept} open solicitations")
    return out


def get_parks_results() -> list[dict]:
    diag = bool(os.environ.get("DIAG"))
    try:
        html = asyncio.run(_fetch_html())
    except Exception as e:
        print(f"[Parks] fetch error: {e}")
        return []
    if diag:
        print(f"[Parks DIAG] html length: {len(html)}")
    return _parse_html(html, diag)


if __name__ == "__main__":
    for item in get_parks_results():
        print(item["extra"], "|", item["due"], "|", item["title"][:70])
