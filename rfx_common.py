"""
rfx_common.py
-------------
Shared helpers used by both rfx_alert.py and rfx_playwright_sources.py:
the keyword/NAICS relevance filter and a standard result shape.

Keeping this in one place means you tune your keyword list ONCE and every
source picks up the change.
"""

import re

# ─── Relevance filter ─────────────────────────────────────────────────────────
# Multi-word / ordinary words are matched as case-insensitive substrings.
PHRASE_KEYWORDS = [
    "traffic", "traffic signal", "signalization", "traffic engineering",
    "intelligent transportation", "roadway", "highway", "intersection",
    "pavement marking", "signing", "work zone", "maintenance of traffic",
    "maintenance and protection of traffic", "construction inspection",
    "resident engineering", "construction support", "transportation",
    "pedestrian", "bicycle", "complete streets", "traffic calming",
    "corridor", "safety study", "road diet", "guiderail", "guide rail",
    "sign structure", "variable message", "capacity analysis",
    "transportation planning", "civil engineering", "engineering services",
    "multimodal", "mobility", "streetscape", "design services",
    "professional engineering", "term agreement", "on-call", "on call",
    # -- ADA / accessibility (added: was missing, caused ADA design-build miss) --
    "curb ramp", "curb ramps", "detectable warning", "pedestrian ramp",
    "ada compliance", "ada transition", "ada upgrade",
    # -- Project-delivery terms (titles often use these instead of a discipline) --
    "design-build", "design build", "design-bid-build",
    "progressive design-build", "task order", "idiq",
]

# Short ALL-CAPS acronyms — matched on word boundaries so we don't hit, e.g.,
# "its" inside "limits" or "mot" inside "motor".
ACRONYM_KEYWORDS = ["ITS", "MOT", "MPT", "WZTC", "CEI", "VMS", "PVMS", "TMC", "RWIS", "ADA"]
_ACRONYM_RE = re.compile(r"\b(" + "|".join(ACRONYM_KEYWORDS) + r")\b")

# Whole-word keywords — case-insensitive but boundary-checked, so "bridge"
# doesn't hit inside "Cambridge" or "Bridgehampton".
WORD_KEYWORDS = ["bridge", "bridges"]
_WORD_RE = re.compile(r"\b(" + "|".join(WORD_KEYWORDS) + r")\b", re.IGNORECASE)

# Engineering Services + Landscape Architecture (used streetscape/multimodal).
NAICS_CODES = ["541330", "541320"]

# Phrases that signal NON-engineering notices (a vendor to *operate* a food/retail
# stand on a plaza, etc.). These are deliberately operator-specific so they can't
# clip plaza/streetscape DESIGN or construction work, which is in scope.
NEGATIVE_PHRASES = [
    "subconcession", "concession", "snack bar", "kiosk", "newsstand",
    "food and beverage", "hospitality", "riding stable", "golf course",
    "market subconcession",
]


def matches(*text_parts) -> bool:
    """True if any provided text contains a relevant keyword/acronym/NAICS code
    AND no negative phrase is present."""
    blob = " ".join(p for p in text_parts if p)
    low = blob.lower()
    if any(neg in low for neg in NEGATIVE_PHRASES):
        return False
    if any(kw in low for kw in PHRASE_KEYWORDS):
        return True
    if _ACRONYM_RE.search(blob):  # case-sensitive-ish boundary check on original
        return True
    if _WORD_RE.search(blob):     # whole-word check (e.g. "bridge", not "Cambridge")
        return True
    if any(code in blob for code in NAICS_CODES):
        return True
    return False


def result(source, agency, title, url, date=None, extra=None, due=None) -> dict:
    """Standard result shape so every source produces identical dicts."""
    return {
        "source": source,      # e.g. "SAM.gov", "PANYNJ Bonfire"
        "agency": agency or "",
        "title": (title or "").strip(),
        "url": url or "",
        "date": date or "",
        "due": due or "Unknown",   # closing/response date, or "Unknown"
        "extra": extra or "",  # solicitation #, reference, etc.
    }
