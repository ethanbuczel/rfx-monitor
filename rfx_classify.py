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


def _fetch_detail_text(url: str) -> str:
    """Best-effort fetch of an opportunity's detail page. Returns plain text,
    or "" on any failure. PDFs and JS-heavy portals will often yield little —
    that's fine, the classifier falls back to the title."""
    if not url or not url.startswith("http"):
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
    source_hint = f"[{item.get('source','')} / {item.get('agency','')}]"
    context = f"Title: {title}\nSource: {source_hint}\n"
    if detail:
        context += f"Detail page excerpt: {detail}\n"
    else:
        context += "Detail page: (could not fetch — judge from title)\n"

    prompt = (
        "You classify government procurement opportunities for a TRAFFIC/CIVIL "
        "ENGINEERING CONSULTING firm. The firm does DESIGN and engineering "
        "services (studies, design, plans, inspection, construction support/"
        "management, professional engineering). It does NOT self-perform "
        "CONSTRUCTION — it is not a contractor that builds, paves, installs, or "
        "supplies materials.\n\n"
        "Decide whether this opportunity is:\n"
        "- \"design\": engineering/design/study/inspection/CM/professional "
        "services — a fit for the firm.\n"
        "- \"construction\": physical construction, paving, installation, "
        "materials supply, equipment, a contractor bid — usually NOT a fit.\n"
        "- \"unknown\": genuinely can't tell.\n\n"
        "Note: design-build solicitations usually seek a contractor-led team; "
        "call those \"construction\" unless the text clearly seeks the design/"
        "engineering role.\n\n"
        f"{context}\n"
        "Respond with ONLY a compact JSON object, no other text:\n"
        '{"v": "design"|"construction"|"unknown", "r": "<reason, max 10 words>"}'
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
        if verdict not in ("design", "construction", "unknown"):
            verdict = "unknown"
        return verdict, str(v.get("r", ""))[:80]
    except Exception:
        return "unknown", ""


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
                    verdicts[idx] = ("unknown", "")
    except Exception as e:
        print(f"[classify] batch failed ({e}) — leaving items unannotated.")
        return results

    n_constr = n_design = 0
    for idx, (verdict, reason) in verdicts.items():
        if verdict == "construction":
            note = "⚠ Likely CONSTRUCTION (not design)"
            if reason:
                note += f" — {reason}"
            n_constr += 1
        elif verdict == "design":
            note = "✓ Likely design/engineering"
            n_design += 1
        else:
            continue  # unknown -> no note
        extra = results[idx].get("extra") or ""
        results[idx]["extra"] = f"{note} | {extra}" if extra else note

    print(f"[classify] tagged {n_constr} likely-construction, "
          f"{n_design} likely-design, "
          f"{len(results) - n_constr - n_design} unmarked, of {len(results)}.")
    return results
