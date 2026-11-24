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


_CHALLENGE = ("just a moment", "security verification", "verify you are human",
              "checking your browser", "enable javascript and cookies",
              "performing security verification")


async def _fetch_html(diag: bool = False) -> str:
    """Load the Parks procurement page in headless Chromium and return the
    RENDERED html. Plain requests gets a 403, AND the page sits behind a
    Cloudflare "Just a moment..." JS interstitial, so we must POLL until that
    challenge clears before reading content — the exact approach that works for
    the Tarrytown village scraper."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=_UA, locale="en-US")
        page = await ctx.new_page()
        try:
            await page.goto(PARKS_URL, timeout=45000,
                            wait_until="domcontentloaded")
            # Poll out Cloudflare: it runs a JS check then swaps in the real
            # page. Break once the challenge text is gone and a procurement
            # number has appeared (or we run out of tries).
            for _ in range(10):
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                body = (await page.inner_text("body")) or ""
                low = body[:600].lower()
                cleared = not any(c in low for c in _CHALLENGE)
                if cleared and (_PNUM_RE.search(body) or len(body.strip()) > 2000):
                    break
                await page.wait_for_timeout(2500)
            html = await page.content()
            if diag:
                head = re.sub(r"\s+", " ", (await page.inner_text("body")))[:160]
                print(f"[Parks DIAG] loaded {len(html)} chars; "
                      f"cloudflare_cleared={not any(c in head.lower() for c in _CHALLENGE)}; "
                      f"head={head!r}")
            return html
        finally:
            await browser.close()


_DOCWORD_RE = re.compile(
    r"\.pdf|\(pdf\)|package|amendment|addendum|attachment|q\s*&\s*a|"
    r"question|answer|exhibit|appendix|sign[\s-]?in|pre[\s-]?bid|map|"
    r"date posted|bid due|due date|vendor question", re.I)
_LABEL_RE = re.compile(r"^(?:ifb|rfp|rfq|rfi|op)\b", re.I)


def _pick_title(block: str, pnum: str) -> str:
    """Pick the human description line out of a solicitation's text block.
    The block also holds the number, document link labels, dates and update
    text, so we take the longest line that is NONE of those."""
    best = ""
    for raw in block.split("\n"):
        t = re.sub(r"\s+", " ", raw).strip()
        if not t or _PNUM_RE.fullmatch(t):
            continue
        # drop the number-only, document-label, and date/label lines
        if _DOCWORD_RE.search(t):
            continue
        if _DATE_RE.search(t) and len(t) < 40:
            continue
        core = _PNUM_RE.sub("", t).strip(" -–:")   # strip a leading "C003761 - "
        if len(core) < 6:
            continue
        if sum(ch.isalpha() for ch in core) > sum(ch.isalpha() for ch in best):
            best = core
    return re.sub(r"^(?:IFB|RFP|RFQ|RFI)\s+", "", best).strip()


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

    # Map each procurement number to its document PDF link (the href or the
    # anchor text usually carries the number, e.g. .../C003761IFBPackage.pdf).
    pdf_by_num = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        m = _PNUM_RE.search(href) or _PNUM_RE.search(a.get_text(" ", strip=True))
        if m:
            num = re.sub(r"\s+", "", m.group(1)).upper()
            pdf_by_num.setdefault(num, _abs(href))

    # Structure-independent: work off the visible text and split it into one
    # block per solicitation at each NEW procurement number (table rows, divs or
    # an accordion all flatten to the same text).
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)

    bounds, last = [], None
    for m in _PNUM_RE.finditer(text):
        num = re.sub(r"\s+", "", m.group(1)).upper()
        if num != last:
            bounds.append((num, m.start()))
            last = num

    if diag:
        print(f"[Parks DIAG] procurement-number blocks: {len(bounds)}")
        if not bounds:
            print("[Parks DIAG] no numbers found — first 1500 chars of text:")
            print(text[:1500])

    kept = 0
    for i, (pnum, start) in enumerate(bounds):
        end = bounds[i + 1][1] if i + 1 < len(bounds) else len(text)
        block = text[start:end]
        low = block.lower()

        if any(w in low for w in ("closed", "withdrawn", "cancelled",
                                  "canceled", "awarded", "rescinded")):
            if diag:
                print(f"[Parks DIAG] skip CLOSED {pnum}")
            continue

        title = _pick_title(block, pnum) or block.split("\n")[0][:120]

        if not _parks_matches(f"{pnum} {title}"):
            if diag:
                print(f"[Parks DIAG] drop junk  {pnum}: {title[:70]!r}")
            continue

        due_str, due_date = _latest_date(block)
        if due_date is not None and due_date < today:
            if diag:
                print(f"[Parks DIAG] skip PASTDUE {pnum} ({due_str}): {title[:55]!r}")
            continue

        url = pdf_by_num.get(pnum, PARKS_URL)

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
        html = asyncio.run(_fetch_html(diag))
    except Exception as e:
        print(f"[Parks] fetch error: {e}")
        return []
    return _parse_html(html, diag)


if __name__ == "__main__":
    for item in get_parks_results():
        print(item["extra"], "|", item["due"], "|", item["title"][:70])
