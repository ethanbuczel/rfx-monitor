"""
rfx_llm_filter.py
Optional LLM relevance pass over the digest results, via OpenRouter.

Substring keywords can't tell that "Design-Build for Substation Replacements"
is power-utility work, or that an oddly worded title is actually a traffic job.
This pass sends the final title list to a cheap LLM and asks which items are
off-scope for a traffic/civil engineering consultant.

DESIGN PRINCIPLES
- FAILS OPEN: any error (no key, network, bad response) returns the list
  unchanged. The digest never breaks because of this module.
- Runs AFTER seen-file tracking in rfx_alert.py, so it only affects the email
  display — NEW-flag history stays complete no matter what the LLM decides.
- When the model is uncertain, it is instructed to KEEP.

MODES (set LLM_MODE env var, or edit DEFAULT_MODE):
  "tag"  (default) - off-scope items stay in the email but get a visible
                     "AI: likely off-scope" note. Use this first to audit
                     the model's judgment for a week or two.
  "drop" - off-scope items are removed from the email (a one-line summary of
           what was dropped is printed to the run log).
  "off"  - skip the pass entirely.

SETUP
  Set env var OPENROUTER_API_KEY (get one at openrouter.ai -> Keys).
  Cost: ~80 titles/run through Claude Haiku is well under a cent per run.
"""

import json
import os
import re

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"   # cheap + good; any OpenRouter model id works
DEFAULT_MODE = "tag"
CHUNK_SIZE = 50                        # titles per API call
TIMEOUT = 60

COMPANY_PROFILE = """You screen procurement listings for a traffic/civil \
engineering consulting firm in the NY metro area (NYSDOT Regions 8/10/11, NYC \
agencies, Long Island towns, NJ, PANYNJ, MTA).

RELEVANT (keep): traffic engineering and signals, signing and pavement \
markings, MOT/WZTC/maintenance and protection of traffic, construction \
staging/support/inspection, highway and roadway design, intersections, \
ADA/pedestrian/bicycle/complete streets, bridges, ITS, roadway drainage, \
transportation planning and studies, on-call/term engineering agreements, \
design-build for any transportation infrastructure, streetscapes.

NOT RELEVANT (drop): building/facility trades (painting, HVAC, roofing, \
windows), water/sewer plant equipment and chemicals, vehicle and equipment \
purchases, IT/software, printing, concessions and leases, landscaping-only, \
playgrounds, power-utility substations, demolition of buildings, \
janitorial/security services, appraisal and financial services.

If a listing is ambiguous or could plausibly involve roadway/traffic scope, \
KEEP it. Only drop items that are clearly outside the firm's practice."""


def _classify_chunk(items: list[dict], offset: int, api_key: str) -> dict[int, str]:
    """Ask the model about one chunk. Returns {global_index: reason} for DROPS
    only. Raises on any failure (caller fails open)."""
    numbered = "\n".join(
        f"{offset + i}. [{r.get('agency') or r.get('source', '')}] {r['title'][:150]}"
        for i, r in enumerate(items)
    )
    prompt = (
        f"{COMPANY_PROFILE}\n\n"
        f"Classify each listing below. Respond with ONLY a JSON array, no other "
        f"text, one object per listing: "
        f'{{"i": <number>, "v": "keep" or "drop", "r": "<reason, max 8 words>"}}\n\n'
        f"{numbered}"
    )
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    # Strip markdown fences if the model added them
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    verdicts = json.loads(content)

    drops = {}
    for v in verdicts:
        if str(v.get("v", "keep")).lower() == "drop":
            drops[int(v["i"])] = str(v.get("r", ""))[:80]
    return drops


def llm_filter(results: list[dict]) -> list[dict]:
    """Main entry point — call with the final deduped list, returns the list
    to email (annotated or reduced depending on mode). Fails open."""
    mode = (os.environ.get("LLM_MODE") or DEFAULT_MODE).lower()
    if mode == "off" or not results:
        return results

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("[llm] OPENROUTER_API_KEY not set — skipping LLM pass.")
        return results

    # Collect drop verdicts across chunks; ANY chunk failure = fail open.
    drops: dict[int, str] = {}
    try:
        for start in range(0, len(results), CHUNK_SIZE):
            chunk = results[start:start + CHUNK_SIZE]
            drops.update(_classify_chunk(chunk, start, api_key))
    except Exception as e:
        print(f"[llm] pass failed ({e}) — keeping all items.")
        return results

    if not drops:
        print(f"[llm] reviewed {len(results)} items, all look in-scope.")
        return results

    if mode == "drop":
        kept = [r for i, r in enumerate(results) if i not in drops]
        print(f"[llm] dropped {len(drops)} of {len(results)} as off-scope:")
        for i, reason in sorted(drops.items()):
            print(f"    - {results[i]['title'][:70]} ({reason})")
        return kept

    # tag mode (default): annotate but keep everything
    for i, reason in drops.items():
        note = f"AI: likely off-scope — {reason}" if reason else "AI: likely off-scope"
        extra = results[i].get("extra") or ""
        results[i]["extra"] = f"{note} | {extra}" if extra else note
    print(f"[llm] tagged {len(drops)} of {len(results)} as likely off-scope "
          f"(tag mode — nothing removed).")
    return results
