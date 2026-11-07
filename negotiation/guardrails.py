"""Agent-side guardrails. Run inside each company's private runtime."""
from __future__ import annotations

import re
from typing import Dict, List

from .models import PrivatePolicy, TermValue
from .policy_service import PolicyService

FORBIDDEN_PHRASES = [
    "minimum", "maximum", "our floor", "floor price", "ceiling", "budget is", "our budget",
    "walk-away", "walk away price", "reservation price", "bottom line", "lowest we",
    "highest we", "most we can", "least we can", "approval threshold", "private", "target price",
]


def secret_numbers(policy: PrivatePolicy) -> List[float]:
    vals = []
    for b in list(policy.terms.values()) + list(policy.non_negotiable.values()):
        vals += [v for v in (b.min, b.max, b.target) if v is not None]
    for r in policy.approval_rules:
        if r.when:
            vals.append(r.when.value)
    return sorted(set(vals))


def _numbers_in(text: str) -> List[float]:
    out = []
    for raw in re.findall(r"\d[\d,]*\.?\d*", text):
        try:
            out.append(float(raw.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    return out


def leak_findings(text: str, policy: PrivatePolicy, offered: Dict[str, TermValue] = None) -> List[str]:
    """Return reasons the text may leak secrets. Numbers equal to values in the
    current public offer are allowed (they are already disclosed)."""
    low = text.lower()
    found = [f"phrase:{p}" for p in FORBIDDEN_PHRASES if p in low]
    found += [f"objective:{o[:30]}" for o in policy.objectives if o.lower() in low]
    allowed = set()
    for v in (offered or {}).values():
        try:
            allowed.add(round(float(v), 2))
        except (TypeError, ValueError):
            pass
    for n in _numbers_in(text):
        if round(n, 2) in {round(s, 2) for s in secret_numbers(policy)} and round(n, 2) not in allowed:
            found.append(f"number:{n:g}")
    return found


def self_check(policy: PrivatePolicy, terms: Dict[str, TermValue]) -> List[str]:
    return PolicyService({policy.company_id: policy}).check(policy.company_id, terms).codes
