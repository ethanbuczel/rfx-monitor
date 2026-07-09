"""
rfx_classify.py
Design-vs-construction classifier for RFx opportunities, via OpenRouter.

WHAT IT DOES
For each opportunity that passed the keyword filter, this tries to determine
whether the work is DESIGN / engineering (what GPI wants) or CONSTRUCTION /
contractor services (usually not a fit). It adds a note to the email — it
NEVER drops anything. You stay in control; the tag is just advice.

Notes it can add (appended to each item's "extra" field):
  "⚠ Likely CONSTRUCTION (not design)"   - probably a contractor bid
  "✓ Likely design/engineering"          - probably a fit (only shown when the
                                            fetch gave enough to be confident)
  (no note)                              - couldn't tell; left unmarked

HOW IT DECIDES
1. Tries to fetch each opportunity's detail page/RFP text (best signal).
2. If the fetch fails/blocks/times out, falls back to the title alone.
3. Sends title + whatever text it got to a cheap LLM for a design/construction
   call with a short reason.

DESIGN PRINCIPLES (same as the relevance filter)
- FAILS OPEN: any error anywhere returns items unchanged, no note. The digest
  never breaks because of this.
- NEVER removes items — annotation only.
- Runs after seen-tracking in rfx_alert.py, so notes don't affect NEW-flagging.

SETUP
  Needs OPENROUTER_API_KEY. If absent, this module skips itself silently.
  Toggle with CLASSIFY_MODE env var: "on" (default) or "off".
  Cost: fetching is free; ~77 short Haiku calls/run is roughly a cent or two.
  Fetching adds time (each detail page is a network round-trip), so this is
  the slowest optional step — budget an extra minute or two per run.
"""

import os
import re
import concurrent.futures

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"   # paid Haiku via OpenRouter
TIMEOUT_LLM = 30
TIMEOUT_FETCH = 20
MAX_FETCH_WORKERS = 6      # parallel detail-page fetches
MAX_TEXT_CHARS = 4000      # how much fetched text to send the LLM

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RFxMonitor/1.0"}


# URLs that are SHARED listing pages, not per-item detail pages. Fetching these
# returns the whole board (all opportunities mixed together), which pollutes the
# classification of any single item with scope language from its neighbors. For
# these, we skip the fetch and judge from the (clean) title alone.
SHARED_LISTING_URLS = (
    "mta.info/agency/construction-and-development/contracting/current-opportunities",
    "bidsapp.townofbabylon.com/Bid?statusId=2",
    "southampton.procureware.com/bids",
    "passport.cityofnewyork.us",   # public board, not per-solicitation
    "bidnetdirect.com",            # bot-blocks fetches AND locks description
                                   # behind login — returns a useless shell, so
                                   # judge from the (clean) title instead
)


def _fetch_detail_text(url: str) -> str:
    """Best-effort fetch of an opportunity's detail page. Returns plain text,
    or "" on any failure. PDFs and JS-heavy portals will often yield little —
    that's fine, the classifier falls back to the title."""
    if not url or not url.startswith("http"):
        return ""
    # Don't fetch shared listing pages — they return every opportunity at once,
    # which would contaminate this item's classification with others' scope.
    if any(s in url for s in SHARED_LISTING_URLS):
        return ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_FETCH)
        if r.status_code != 200:
            return ""
        ctype = r.headers.get("Content-Type", "").lower()
        if "pdf" in ctype:
            # Don't parse PDFs here (keeps deps light); signal it's a PDF so the
            # LLM knows to lean on the title.
            return ""
        try:
            from bs4 import BeautifulSoup
            text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        except ImportError:
            text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_TEXT_CHARS]
    except Exception:
        return ""


def _classify_one(item: dict, api_key: str) -> tuple[str, str]:
    """Return (verdict, reason) for one item. verdict in
    {"design", "construction", "unknown"}. Never raises — returns
    ("unknown", "") on any problem, so the caller can fail open."""
    title = item.get("title", "")
    detail = _fetch_detail_text(item.get("url", ""))
    had_detail = bool(detail)
    source_hint = f"[{item.get('source','')} / {item.get('agency','')}]"
    context = f"Title: {title}\nSource: {source_hint}\n"
    if detail:
        context += f"Detail page excerpt: {detail}\n"
    else:
        context += (
            "Detail page: COULD NOT BE FETCHED. You have ONLY the title above. "
            "Do NOT invent or assume specific scope details you cannot see. In "
            "particular, do NOT infer 'interior' or 'building' scope just "
            "because a place name (e.g. 'Town Hall', 'Station', 'Courthouse') "
            "appears — an 'ADA Ramp' or 'sidewalk' at such a location is often "
            "PEDESTRIAN/roadway work, which IS relevant. Judge only from what "
            "the title actually states, and when it names a transportation, "
            "roadway, pedestrian/ADA-ramp, bridge, or traffic term, lean "
            "RELEVANT. If the title is truly too vague to tell, answer "
            "'unknown'.\n")

    prompt = (
        "You screen government procurement opportunities for a TRAFFIC "
        "ENGINEERING consulting firm. Decide whether the opportunity is a "
        "POTENTIAL CONTRACT for them.\n\n"
        "The test is deliberately BROAD: mark it a potential contract if there "
        "is ANY traffic or transportation scope the firm could perform — this "
        "INCLUDES not just design/plans but also construction inspection (CEI), "
        "construction support, construction management, resident engineering, "
        "studies, and inspection/inventory of transportation assets. The firm "
        "does these phases too, so do NOT reject something merely because it is "
        "'inspection' or 'construction support' rather than pure design.\n\n"
        "IMPORTANT clarifications (these were being misjudged):\n"
        "- ADA work in a ROADWAY/TRANSIT context (curb ramps, pedestrian "
        "ramps, detectable warnings, sidewalk/pedestrian accessibility) IS "
        "traffic/transportation design work → RELEVANT. Only treat ADA as "
        "not-relevant when it's clearly about building interiors (elevators, "
        "restrooms, doorways).\n"
        "- Construction Inspection / CEI / construction support / resident "
        "engineering for roadway, highway, bridge, or transportation projects "
        "IS in scope → RELEVANT.\n"
        "- Bridge, highway, and roadway projects (design, inspection, or "
        "design-build) → RELEVANT, since traffic engineering (signals, "
        "signing, markings, MOT) is part of that work.\n\n"
        "RELEVANT examples: traffic signals/signalization, signing and "
        "pavement markings, maintenance and protection of traffic (MOT/MPT/"
        "WZTC), traffic and safety studies, corridor/intersection work, ITS, "
        "pedestrian/bicycle/complete-streets, ADA curb ramps, roadway/highway/"
        "bridge design or inspection, transportation planning, construction "
        "inspection/support for transportation projects, survey or term "
        "agreements tied to transportation work.\n\n"
        "NOT RELEVANT examples (no traffic/transportation scope): building "
        "renovations and envelopes, interior/elevator/restroom ADA, HVAC/"
        "roofing/painting/waterproofing, water/sewer/PFAS treatment, marine/"
        "port/waterfront, electrical/mechanical/geotechnical/structural design "
        "for buildings, asbestos/lead abatement, radio/IT/software, real "
        "estate, appraisals, concessions, janitorial or security services, "
        "landscaping-only.\n\n"
        "When in doubt, lean RELEVANT — it is better to surface a maybe than "
        "hide a real one.\n\n"
        f"{context}\n"
        "Respond with ONLY a compact JSON object, no other text:\n"
        '{"v": "relevant"|"not_relevant"|"unknown", "r": "<reason, max 10 words>"}'
    )
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": MODEL,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0},
            timeout=TIMEOUT_LLM,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        import json
        v = json.loads(content)
        verdict = str(v.get("v", "unknown")).lower()
        if verdict not in ("relevant", "not_relevant", "unknown"):
            verdict = "unknown"
        return verdict, str(v.get("r", ""))[:80], had_detail
    except Exception:
        return "unknown", "", had_detail


def classify_results(results: list[dict]) -> list[dict]:
    """Annotate each item with a design/construction note. Fails open."""
    mode = (os.environ.get("CLASSIFY_MODE") or "on").lower()
    if mode == "off" or not results:
        return results

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("[classify] OPENROUTER_API_KEY not set — skipping.")
        return results

    verdicts = {}
    try:
        # Fetch + classify in parallel; each _classify_one is self-contained
        # and never raises, so one bad item can't sink the batch.
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as ex:
            futures = {ex.submit(_classify_one, item, api_key): idx
                       for idx, item in enumerate(results)}
            for fut in concurrent.futures.as_completed(futures, timeout=300):
                idx = futures[fut]
                try:
                    verdicts[idx] = fut.result()
                except Exception:
                    verdicts[idx] = ("unknown", "", False)
    except Exception as e:
        print(f"[classify] batch failed ({e}) — leaving items unannotated.")
        return results

    n_notrel = n_rel = 0
    for idx, (verdict, reason, had_detail) in verdicts.items():
        item = results[idx]
        if verdict == "not_relevant":
            item["relevance"] = "not_relevant"
            item["relevance_reason"] = reason or ""
            n_notrel += 1
        elif verdict == "relevant":
            item["relevance"] = "relevant"
            item["relevance_reason"] = ""
            n_rel += 1
        else:
            item["relevance"] = "unknown"
            item["relevance_reason"] = ""
            continue
        item["relevance_title_only"] = not had_detail

    # Any item the classifier didn't return a verdict for -> unknown
    for it in results:
        it.setdefault("relevance", "unknown")

    print(f"[classify] tagged {n_rel} potential-contract, "
          f"{n_notrel} not-relevant, "
          f"{len(results) - n_rel - n_notrel} unmarked, of {len(results)}.")
    return results
