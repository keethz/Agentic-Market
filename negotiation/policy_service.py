"""Policy Service: the only component holding BOTH companies' secret limits.

It answers pass/fail with generic reason codes and never returns raw limits.
Use `PolicyService` in-process, or run `policy_api.py` to expose it over HTTP.
"""
from __future__ import annotations

import operator
from typing import Dict, List

from pydantic import BaseModel

from .models import TERM_SCHEMA, PrivatePolicy, TermBounds, TermValue, total_value

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}


class CheckResult(BaseModel):
    ok: bool
    codes: List[str] = []   # e.g. ["OUT_OF_RANGE", "NON_NEGOTIABLE_VIOLATED"] — no numbers


def _within(value: TermValue, b: TermBounds) -> bool:
    if b.allowed is not None:
        return str(value).lower() in [a.lower() for a in b.allowed]
    v = float(value)
    if b.min is not None and v < b.min:
        return False
    if b.max is not None and v > b.max:
        return False
    return True


def validate_schema(terms) -> List[str]:
    if not isinstance(terms, dict):
        return ["MISSING_TERMS"]
    codes = []
    for k, kind in TERM_SCHEMA.items():
        if k not in terms:
            codes.append("MISSING_TERM")
            continue
        try:
            if kind in ("number", "integer"):
                float(terms[k])
        except (TypeError, ValueError):
            codes.append("BAD_TYPE")
    extra = set(terms) - set(TERM_SCHEMA)
    if extra:
        codes.append("UNKNOWN_TERM")
    return sorted(set(codes))


class PolicyService:
    def __init__(self, policies: Dict[str, PrivatePolicy]):
        self._p = policies

    def check(self, company_id: str, terms: Dict[str, TermValue]) -> CheckResult:
        codes = validate_schema(terms)
        if codes:
            return CheckResult(ok=False, codes=codes)
        pol = self._p[company_id]
        for k, b in pol.non_negotiable.items():
            if not _within(terms[k], b):
                codes.append("NON_NEGOTIABLE_VIOLATED")
        for k, b in pol.terms.items():
            if not _within(terms[k], b):
                codes.append("OUT_OF_RANGE")
        codes = sorted(set(codes))
        return CheckResult(ok=not codes, codes=codes)

    def approvers(self, company_id: str, terms: Dict[str, TermValue]) -> List[str]:
        facts = dict(terms, total_value=total_value(terms))
        roles = []
        for r in self._p[company_id].approval_rules:
            if r.when is None or OPS[r.when.op](float(facts[r.when.field]), r.when.value):
                roles.append(r.role)
        return roles
