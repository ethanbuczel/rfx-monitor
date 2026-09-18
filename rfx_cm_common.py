"""
rfx_cm_common.py — keyword filter for CONSTRUCTION MANAGEMENT / VERTICAL work.

Parallel to rfx_common.matches(), but tuned for CM / owner's-rep / PMC /
commissioning / MEP / building-systems work instead of traffic/roadway design.

Design philosophy (learned from the traffic pipeline): keywords catch
CANDIDATES generously; the AI classifier (rfx_cm_classify) does the real
judging. So this leans a bit wide — BUT the dangerous short acronyms ("CM",
"PMC") are handled carefully so they don't match centimeters, reference
numbers, or random letter pairs.

cm_matches(*text_parts) -> bool   # True if the text looks like a CM candidate
"""

import re

# --- Safe, specific CM/vertical phrases (plain substring, lowercased) --------
CM_PHRASES = [
    "construction management",
    "construction manager",
    "cm as agent",
    "cm-as-agent",
    "cm at risk",
    "cm/gc",
    "construction management services",
    "owner's rep", "owners rep", "owner's representative",
    "owners representative", "owner representative",
    "project management consultant",
    "program management",
    "commissioning",              # incl. re/retro-commissioning
    "mep design", "mep engineering", "mechanical electrical plumbing",
    "building systems",
    "building envelope",
    "facilities management",
    "capital construction",
    "vertical construction",
    "construction administration",
    "construction inspection services",   # (also traffic, but CM-relevant)
    "resident engineer",
    "clerk of the works",
    "cost estimating",
    "scheduling services",
    "constructability",
]

# --- Dangerous short acronyms: require CM-CONTEXT, never match bare ----------
# "CM" and "PMC" alone match far too much (centimeters, IDs, etc.). Only count
# them when they appear next to a service/role word, as a real solicitation
# would phrase them. These regexes run on the ORIGINAL text (case-sensitive-ish
# via \b and uppercase) to avoid matching lowercase noise.
_CM_CONTEXT_RE = re.compile(
    r"\bCM\b[\s\-/]{0,3}"
    r"(?:services|consultant|consulting|firm|agent|at[\s\-]risk|/?gc|"
    r"provider|team|contract|assistance|support)",
    re.IGNORECASE,
)
# "CM services", "CM as Agent", "CM/GC", "CM at Risk", "CM consultant", etc.
_PMC_RE = re.compile(r"\bPMC\b")   # PMC is distinctive enough as a bare acronym,
                                   # but still boundary-locked so it won't match
                                   # inside longer strings.

# --- Negative phrases: strip obvious non-CM even if a keyword matched --------
# (Kept short — the AI classifier is the real filter. These only kill the
# clearest false-friends.)
CM_NEGATIVE_PHRASES = [
    "snow removal", "janitorial", "landscaping services only",
    "lawn care", "pest control", "security guard services",
    "uniform rental", "vending", "catering",
]


def cm_matches(*text_parts) -> bool:
    """True if the text looks like a Construction-Management / vertical
    candidate. Generous by design — the CM classifier does the fine sorting."""
    blob = " ".join(p for p in text_parts if p)
    if not blob:
        return False
    low = blob.lower()

    if any(neg in low for neg in CM_NEGATIVE_PHRASES):
        return False

    if any(kw in low for kw in CM_PHRASES):
        return True
    if _CM_CONTEXT_RE.search(blob):
        return True
    if _PMC_RE.search(blob):
        return True
    return False
