"""Shared data models. Public models may appear in BAND; Private* models must not."""
from __future__ import annotations

import enum
import hashlib
import json
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

TermValue = Union[float, int, str]

# Canonical term sheet both sides negotiate against.
TERM_SCHEMA: Dict[str, str] = {
    "unit_price": "number",        # USD per seat per year
    "quantity": "integer",         # seats
    "contract_months": "integer",
    "payment_terms_days": "integer",
    "sla_uptime": "number",        # percent
    "support_tier": "enum",        # standard | premium
}
SUPPORT_TIERS = ["standard", "premium"]


def total_value(terms: Dict[str, TermValue]) -> float:
    return round(float(terms["unit_price"]) * int(terms["quantity"]) * int(terms["contract_months"]) / 12, 2)


def normalize_terms(terms: Dict[str, TermValue]) -> Dict[str, TermValue]:
    """Coerce types so equality checks are exact and deterministic."""
    out: Dict[str, TermValue] = {}
    for k, kind in TERM_SCHEMA.items():
        v = terms[k]
        if kind == "number":
            out[k] = round(float(v), 2)
        elif kind == "integer":
            out[k] = int(v)
        else:
            out[k] = str(v).lower()
    return out


class PublicProfile(BaseModel):
    id: str
    name: str
    role: Literal["seller", "buyer"]
    industry: str
    description: str
    product: Optional[str] = None
    product_needed: Optional[str] = None


class TermBounds(BaseModel):
    min: Optional[float] = None
    max: Optional[float] = None
    target: Optional[float] = None
    allowed: Optional[List[str]] = None


class ApprovalCondition(BaseModel):
    field: str
    op: Literal[">", ">=", "<", "<="]
    value: float


class ApprovalRule(BaseModel):
    role: str
    when: Optional[ApprovalCondition] = None


class PrivatePolicy(BaseModel):
    company_id: str
    objectives: List[str]
    terms: Dict[str, TermBounds]
    non_negotiable: Dict[str, TermBounds] = Field(default_factory=dict)
    approval_rules: List[ApprovalRule] = Field(default_factory=list)


class ProposalType(str, enum.Enum):
    offer = "offer"
    counter = "counter"
    accept = "accept"
    reject = "reject"
    walk_away = "walk_away"


class Proposal(BaseModel):
    """What an agent sends to the coordinator (as JSON text in a BAND DM)."""
    type: ProposalType
    terms: Optional[Dict[str, TermValue]] = None
    rationale: str = ""
    # Non-secret telemetry from the agent's own guardrails (cumulative counts only).
    meta: Optional[Dict[str, int]] = None


class Status(str, enum.Enum):
    negotiating = "NEGOTIATING"
    tentative = "TENTATIVE_AGREEMENT"
    pending_approval = "PENDING_APPROVAL"
    finalized = "FINALIZED"
    rejected = "REJECTED"
    impasse = "IMPASSE"


class Approval(BaseModel):
    company_id: str
    role: str
    approver: Optional[str] = None
    decision: Optional[Literal["approved", "rejected"]] = None
    comment: str = ""
    at: Optional[str] = None


class Contract(BaseModel):
    contract_id: str
    negotiation_id: str
    parties: Dict[str, str]
    terms: Dict[str, TermValue]
    total_value: float
    terms_hash: str
    rounds: int
    created_at: str
    status: Status = Status.pending_approval
    approvals: List[Approval] = Field(default_factory=list)

    @staticmethod
    def hash_terms(terms: Dict[str, TermValue]) -> str:
        return hashlib.sha256(json.dumps(terms, sort_keys=True).encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()
