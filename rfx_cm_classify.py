"""
rfx_cm_classify.py — CONSTRUCTION MANAGEMENT / VERTICAL classifier.

This does NOT duplicate the classifier engine. It reuses ALL of rfx_classify's
machinery (detail fetching, PDF reading, threading, title-only safeguard,
sub-work detection) and only swaps in a CM-focused domain prompt. So when the
shared engine is improved, the CM pipeline benefits automatically.

Usage:
    import rfx_cm_classify
    results = rfx_cm_classify.classify_results(results)
"""

import rfx_classify


def _cm_prompt(context: str) -> str:
    """Domain prompt for CONSTRUCTION MANAGEMENT / vertical / owner's-rep work."""
    return (
        "You screen government procurement opportunities for a CONSTRUCTION "
        "MANAGEMENT / vertical-buildings consulting firm (construction "
        "management, owner's representative, program/project management, "
        "commissioning, MEP, building systems). Decide whether the opportunity "
        "is a POTENTIAL CONTRACT for them.\n\n"
        "The test is deliberately BROAD: mark it a potential contract if there "
        "is ANY construction-management or vertical-buildings scope the firm "
        "could perform. This is a DIFFERENT firm from a roadway/traffic "
        "designer — here VERTICAL / building work IS the target, not the "
        "exclusion.\n\n"
        "RELEVANT (construction management / vertical) examples:\n"
        "- Construction Management (CM), CM as Agent, CM at Risk, CM/GC\n"
        "- Owner's Representative / Owner's Rep services\n"
        "- Program Management, Project Management Consultant (PMC)\n"
        "- Commissioning / retro-commissioning agent\n"
        "- MEP design/engineering (mechanical, electrical, plumbing)\n"
        "- Building systems, building envelope, facilities/capital "
        "construction\n"
        "- Construction administration, resident engineering, clerk of the "
        "works, inspection, cost estimating, scheduling, constructability "
        "for BUILDING/VERTICAL projects\n"
        "- New construction, renovation, or additions of buildings "
        "(schools, courthouses, hospitals, offices, fire stations, libraries, "
        "recreation centers, treatment-plant BUILDINGS, garages/parking "
        "structures, transit facilities/stations as structures)\n\n"
        "NOT RELEVANT examples (no CM/vertical scope): pure roadway/pavement "
        "resurfacing or striping with no building, traffic signal design, "
        "landscaping-only, snow removal, janitorial, security guards, uniform/"
        "vending/catering supply, IT/software, real estate/appraisal, "
        "concessions, commodity goods purchases.\n\n"
        "NOTE: a project can be BOTH (e.g. a parking structure or transit "
        "station has vertical construction) — if there is vertical/building CM "
        "scope, mark RELEVANT.\n\n"
        "When in doubt, lean RELEVANT — it is better to surface a maybe than "
        "hide a real one.\n\n"
        "SEPARATELY, detect a SUBCONTRACTOR / FIELD-WORK pattern: a prime firm "
        "that ALREADY HOLDS the contract seeking a sub for a narrow physical/"
        "field task (testing, inspection labor, a single trade) rather than the "
        "management/design role itself. Set \"sub\": true when you see it "
        "(still keep it relevant); otherwise \"sub\": false.\n\n"
        f"{context}\n"
        "Respond with ONLY a compact JSON object, no other text:\n"
        '{"v": "relevant"|"not_relevant"|"unknown", "sub": true|false, '
        '"r": "<reason, max 10 words>"}'
    )


def classify_results(results: list) -> list:
    """Classify with the CM prompt, reusing rfx_classify's full engine."""
    rfx_classify.set_prompt_builder(_cm_prompt)
    try:
        return rfx_classify.classify_results(results)
    finally:
        # restore default so nothing else in the process is affected
        rfx_classify.set_prompt_builder(rfx_classify._traffic_prompt)
