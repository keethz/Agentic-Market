"""Consent-gated outreach. A seller agent may only open a negotiation after the
buyer side opts in. Sellers can't blast every buyer: they need an eligible,
ranked match, and the buyer's PRIVATE consent policy decides accept / decline /
human review. Decline reasons are generic so the policy isn't leaked."""
from __future__ import annotations

import uuid
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel

from .config import CONFIG
from .matchmaker import Match, evaluate
from .models import now
from .registry import Registry

Decision = Literal["pending", "accepted", "declined", "needs_human"]


class ConsentPolicy(BaseModel):
    min_match_score: float = 0.6
    human_review_band: float = 0.45
    blocked_vendors: List[str] = []
    max_conversations: int = 3


def load_consent(buyer_id: str) -> ConsentPolicy:
    for folder in ("consent", "consent.example"):
        p = CONFIG / folder / f"{buyer_id}.yaml"
        if p.exists():
            return ConsentPolicy(**yaml.safe_load(p.read_text()))
    return ConsentPolicy()


class OutreachRequest(BaseModel):
    request_id: str
    need_id: str
    vendor_id: str
    buyer_id: str
    pitch: str
    score: float
    reasons: List[str]
    decision: Decision = "pending"
    decided_by: Optional[str] = None
    at: str


class OutreachDesk:
    MAX_PENDING_PER_VENDOR = 3

    def __init__(self, registry: Registry, policies: Optional[Dict[str, ConsentPolicy]] = None):
        self.registry = registry
        self.policies = policies or {}
        self.requests: Dict[str, OutreachRequest] = {}

    def _policy(self, buyer_id: str) -> ConsentPolicy:
        if buyer_id not in self.policies:
            self.policies[buyer_id] = load_consent(buyer_id)
        return self.policies[buyer_id]

    def request(self, vendor_id: str, need_id: str, pitch: str = "") -> OutreachRequest:
        need = self.registry.needs.get(need_id)
        if not need or need.status != "open":
            raise ValueError("need is not open")
        if vendor_id not in self.registry.vendors:
            raise ValueError("unknown vendor")
        if any(r.vendor_id == vendor_id and r.need_id == need_id for r in self.requests.values()):
            raise ValueError("already requested")
        if sum(r.vendor_id == vendor_id and r.decision in ("pending", "needs_human")
               for r in self.requests.values()) >= self.MAX_PENDING_PER_VENDOR:
            raise ValueError("too many pending outreach requests")
        m: Match = evaluate(need, self.registry.vendors[vendor_id])
        if not m.eligible:
            raise ValueError("not eligible: " + "; ".join(m.gaps))
        r = OutreachRequest(request_id="out-" + uuid.uuid4().hex[:6], need_id=need_id,
                            vendor_id=vendor_id, buyer_id=need.buyer_id, pitch=pitch[:280],
                            score=m.score, reasons=m.reasons, at=now())
        self.requests[r.request_id] = r
        self._auto_decide(r)
        return r

    def _auto_decide(self, r: OutreachRequest):
        """Buyer agent applies its private consent policy."""
        pol = self._policy(r.buyer_id)
        active = sum(x.need_id == r.need_id and x.decision == "accepted" for x in self.requests.values())
        if r.vendor_id in pol.blocked_vendors or active >= pol.max_conversations:
            r.decision, r.decided_by = "declined", "buyer agent"
        elif r.score >= pol.min_match_score:
            r.decision, r.decided_by = "accepted", "buyer agent"
        elif r.score >= pol.human_review_band:
            r.decision = "needs_human"
        else:
            r.decision, r.decided_by = "declined", "buyer agent"

    def decide(self, request_id: str, accept: bool, human: str) -> OutreachRequest:
        r = self.requests[request_id]
        if r.decision != "needs_human":
            raise ValueError(f"request is {r.decision}")
        r.decision, r.decided_by = ("accepted" if accept else "declined"), human
        return r

    def has_consent(self, vendor_id: str, need_id: str) -> bool:
        return any(r.vendor_id == vendor_id and r.need_id == need_id and r.decision == "accepted"
                   for r in self.requests.values())

    def public_view(self, r: OutreachRequest) -> str:
        """What may be shown in a room: no thresholds, no policy details."""
        v = self.registry.vendors[r.vendor_id].name
        b = self.registry.needs[r.need_id].buyer
        word = {"accepted": "✅ accepted", "declined": "🚫 declined (buyer not interested at this time)",
                "needs_human": "⏳ awaiting buyer review", "pending": "pending"}[r.decision]
        return f"Outreach `{r.request_id}`: {v} → {b} ({r.need_id}) — {word}" + \
               (f" by {r.decided_by}" if r.decided_by and r.decision != "declined" else "")
