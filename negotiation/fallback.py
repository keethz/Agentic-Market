"""Vendor fallback: when a negotiation ends in IMPASSE, move to the next-ranked
eligible vendor for the same buyer need — still going through the consent step.

Order comes from the matchmaker. A vendor is skipped if it was already tried,
has no negotiating agent, or the buyer declines outreach. If the buyer's policy
sends the request to human review, fallback pauses until `consent <id> yes|no`."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional

from .config import has_agent
from .matchmaker import rank
from .models import Status
from .outreach import OutreachDesk, OutreachRequest
from .registry import Registry


@dataclass
class NextStep:
    vendor_id: Optional[str] = None            # vendor to negotiate with now
    waiting: Optional[OutreachRequest] = None  # paused on human consent review
    notes: List[str] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.vendor_id is None and self.waiting is None


def next_vendor(registry: Registry, desk: OutreachDesk, need_id: str, tried: Iterable[str],
                available: Optional[Callable[[str], bool]] = None) -> NextStep:
    available = available or has_agent
    tried, step = set(tried), NextStep()
    for m in rank(registry, need_id):
        if m.vendor_id in tried:
            continue
        if not available(m.vendor_id):
            step.notes.append(f"{m.vendor} skipped: no negotiating agent connected")
            continue
        if desk.has_consent(m.vendor_id, need_id):
            step.vendor_id = m.vendor_id
            return step
        existing = next((r for r in desk.requests.values()
                         if r.vendor_id == m.vendor_id and r.need_id == need_id), None)
        try:
            r = existing or desk.request(m.vendor_id, need_id, f"fallback outreach (score {m.score:.2f})")
        except ValueError as e:
            step.notes.append(f"{m.vendor} skipped: {e}")
            continue
        step.notes.append(desk.public_view(r))
        if r.decision == "accepted":
            step.vendor_id = m.vendor_id
            return step
        if r.decision == "needs_human":
            step.waiting = r
            return step
    return step


@dataclass
class Attempt:
    vendor_id: str
    status: str
    negotiation_id: str
    contract_id: Optional[str] = None


def run_with_fallback(need_id: str, brain: str = "scripted", registry: Optional[Registry] = None,
                      desk: Optional[OutreachDesk] = None, brains=None, max_rounds: int = 16, **kw):
    """Local driver: negotiate vendor by vendor until one reaches agreement.
    Returns (attempts, final_coordinator_or_None, last NextStep)."""
    from .system import build
    registry = registry or Registry.load()
    desk = desk or OutreachDesk(registry)
    buyer = registry.needs[need_id].buyer_id
    attempts: List[Attempt] = []
    coord = None
    while True:
        step = next_vendor(registry, desk, need_id, [a.vendor_id for a in attempts])
        if not step.vendor_id:
            return attempts, None, step
        t, coord, *_ = build(brain, seller=step.vendor_id, buyer=buyer, max_rounds=max_rounds,
                             brains=(brains or {}).get(step.vendor_id), **kw)
        coord.start()
        t.run_until_idle()
        attempts.append(Attempt(step.vendor_id, coord.status.value, coord.negotiation_id,
                                coord.contract.contract_id if coord.contract else None))
        if coord.status != Status.impasse:
            return attempts, coord, step
