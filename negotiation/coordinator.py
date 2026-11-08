"""Negotiation Coordinator — deterministic, no LLM.

The ONLY component allowed to: relay messages between companies, change status,
declare agreement, request human approval, and create the contract record.
It never sees raw limits: constraint checks go through the Policy Service.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import ValidationError

from . import analytics

from .models import (Approval, Contract, Proposal, ProposalType, PublicProfile, Status,
                     normalize_terms, now, total_value)
from .policy_service import PolicyService, validate_schema
from .transport import Message, Transport

COORDINATOR = "coordinator"
DATA = Path(__file__).resolve().parent.parent / "data"


class Coordinator:
    def __init__(self, transport: Transport, policy: PolicyService,
                 a: PublicProfile, b: PublicProfile, max_rounds: int = 16,
                 data_dir: Path = DATA):
        self.t, self.policy = transport, policy
        self.parties = {a.id: a, b.id: b}
        self.other = {a.id: b.id, b.id: a.id}
        self.max_rounds = max_rounds
        self.negotiation_id = "neg-" + uuid.uuid4().hex[:8]
        self.status = Status.negotiating
        self.round = 0
        self.expected: str = a.id  # seller opens
        self.last_terms: Dict[str, Optional[dict]] = {a.id: None, b.id: None}
        self.pending_accept: Optional[dict] = None  # {"by": cid, "terms": {...}}
        self.contract: Optional[Contract] = None
        self.audit: List[dict] = []
        self.agent_meta: Dict[str, Optional[dict]] = {a.id: None, b.id: None}
        self.analytics: Optional[dict] = None
        self.contract_pdf: Optional[Path] = None
        self.data_dir = data_dir
        self.t.subscribe(COORDINATOR, self.on_message)

    # ---------- helpers ----------
    def _log(self, event: str, **kw):
        rec = {"at": now(), "negotiation_id": self.negotiation_id, "event": event, **kw}
        self.audit.append(rec)
        self.data_dir.mkdir(exist_ok=True)
        with open(self.data_dir / "audit.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _set_status(self, s: Status, reason: str = ""):
        self.status = s
        self._log("status", status=s.value, reason=reason)
        self.t.post(COORDINATOR, json.dumps({"kind": "status", "status": s.value, "round": self.round, "reason": reason}))
        if s in (Status.pending_approval, Status.impasse, Status.finalized, Status.rejected):
            self._publish_analytics()

    def _publish_analytics(self):
        """Compute, store and post deal metrics at the end of negotiation and again
        when humans finalize or reject the contract."""
        seller = next(c for c, p in self.parties.items() if p.role == "seller")
        buyer = self.other[seller]
        self.analytics = analytics.compute(self.audit, seller, buyer, self.agent_meta,
                                           self.contract.terms if self.contract else None)
        analytics.save(self.data_dir, self.negotiation_id, self.analytics)
        self.t.post(COORDINATOR, json.dumps({"kind": "analytics", "negotiation_id": self.negotiation_id,
                                             "metrics": self.analytics}))

    def _reply_error(self, to: str, codes: List[str]):
        self._log("rejected_message", sender=to, codes=codes)
        self.t.dm(COORDINATOR, to, json.dumps({"kind": "error", "codes": codes, "round": self.round,
                                               "your_turn": self.expected == to}))

    # ---------- lifecycle ----------
    def start(self):
        self._set_status(Status.negotiating, "opened")
        a = self.expected
        self.t.dm(COORDINATOR, a, json.dumps({
            "kind": "start", "round": 0, "your_turn": True,
            "counterparty": self.parties[self.other[a]].model_dump(exclude_none=True)}))

    def on_message(self, m: Message):
        sender = m.sender
        if sender not in self.parties:
            return  # humans/others: approvals come through approve()/reject()
        if self.status != Status.negotiating:
            return self._reply_error(sender, ["NEGOTIATION_CLOSED"])
        if sender != self.expected:
            return self._reply_error(sender, ["NOT_YOUR_TURN"])
        try:
            p = Proposal(**json.loads(m.content))
        except (ValueError, ValidationError):
            return self._reply_error(sender, ["INVALID_MESSAGE"])
        if p.meta:
            self.agent_meta[sender] = p.meta

        if p.type == ProposalType.walk_away:
            self._log("walk_away", sender=sender)
            return self._set_status(Status.impasse, f"{self.parties[sender].name} walked away")

        if p.type in (ProposalType.offer, ProposalType.counter, ProposalType.accept):
            codes = validate_schema(p.terms)
            if codes:
                return self._reply_error(sender, codes)
            p.terms = normalize_terms(p.terms)
            # Sender must respect its OWN policy. We do not check the receiver's
            # policy on offers — rejecting on it would leak their limits.
            own = self.policy.check(sender, p.terms)
            if not own.ok:
                return self._reply_error(sender, own.codes)

        if p.type == ProposalType.accept:
            # Second explicit acceptance on identical terms -> agreement.
            if self.pending_accept and self.pending_accept["by"] != sender:
                if p.terms != self.pending_accept["terms"]:
                    return self._reply_error(sender, ["ACCEPT_TERMS_MISMATCH"])
                return self._agree(p.terms)
            # First acceptance must match the other side's latest offer exactly.
            if p.terms != self.last_terms[self.other[sender]]:
                return self._reply_error(sender, ["ACCEPT_TERMS_MISMATCH"])
            self.pending_accept = {"by": sender, "terms": p.terms}
            self._log("accept", sender=sender, round=self.round, terms=p.terms)
            self.expected = self.other[sender]
            self.t.dm(COORDINATOR, self.expected, json.dumps({
                "kind": "confirm_request", "from": self.parties[sender].name, "round": self.round,
                "terms": p.terms, "your_turn": True,
                "note": "Counterparty accepted your offer. Send accept with identical terms to confirm, or counter."}))
            return

        self.pending_accept = None

        # offer / counter / reject -> forward (sanitized) to the other side
        self.round += 1
        if p.terms:
            self.last_terms[sender] = p.terms
        self._log("proposal", sender=sender, round=self.round, type=p.type.value,
                  terms=p.terms, rationale=p.rationale)
        if self.round >= self.max_rounds:
            return self._set_status(Status.impasse, "round limit reached")
        self.expected = self.other[sender]
        self.t.dm(COORDINATOR, self.expected, json.dumps({
            "kind": "proposal", "from": self.parties[sender].name, "round": self.round,
            "type": p.type.value, "terms": p.terms, "rationale": p.rationale, "your_turn": True,
            "rounds_left": self.max_rounds - self.round}))

    # ---------- agreement & contract ----------
    def _agree(self, terms: dict):
        # Both explicit acceptances on identical terms: the sender accepted exactly
        # the other side's latest offer (which the other side proposed = agreed to).
        for cid in self.parties:
            r = self.policy.check(cid, terms)
            if not r.ok:
                self._reply_error(self.expected, ["CONSTRAINT_VIOLATED"])
                return
        self._set_status(Status.tentative, "identical terms accepted by both parties")
        approvals = [Approval(company_id=cid, role=role)
                     for cid in self.parties for role in self.policy.approvers(cid, terms)]
        self.contract = Contract(
            contract_id="ctr-" + uuid.uuid4().hex[:8], negotiation_id=self.negotiation_id,
            parties={cid: p.name for cid, p in self.parties.items()}, terms=terms,
            total_value=total_value(terms), terms_hash=Contract.hash_terms(terms),
            rounds=self.round, created_at=now(), approvals=approvals)
        self._save_contract()
        self._log("contract_created", contract_id=self.contract.contract_id)
        self._set_status(Status.pending_approval, "awaiting human approval")
        self.t.post(COORDINATOR, json.dumps({
            "kind": "approval_request", "contract_id": self.contract.contract_id,
            "terms": terms, "total_value": self.contract.total_value,
            "terms_hash": self.contract.terms_hash, "constraints_satisfied": True,
            "required_approvers": [{"company": self.parties[a.company_id].name, "role": a.role}
                                   for a in approvals]}))

    def _save_contract(self):
        d = self.data_dir / "contracts"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.contract.contract_id}.json").write_text(self.contract.model_dump_json(indent=2))

    def _decide(self, company_id: str, role: str, approver: str, decision: str, comment: str):
        if self.status != Status.pending_approval or not self.contract:
            raise RuntimeError("no contract awaiting approval")
        slot = next((a for a in self.contract.approvals
                     if a.company_id == company_id and a.role == role and a.decision is None), None)
        if slot is None:
            raise ValueError(f"no pending approval for {company_id}/{role}")
        slot.approver, slot.decision, slot.comment, slot.at = approver, decision, comment, now()
        self._log("approval", contract_id=self.contract.contract_id, company=company_id,
                  role=role, approver=approver, decision=decision)
        if decision == "rejected":
            self.contract.status = Status.rejected
            self._set_status(Status.rejected, f"{role} rejected: {comment}")
        elif all(a.decision == "approved" for a in self.contract.approvals):
            self.contract.status = Status.finalized
            self._set_status(Status.finalized, "all approvals received")
        self._save_contract()
        if self.contract.status == Status.finalized:
            self._make_pdf()

    def _make_pdf(self):
        from .contract_pdf import render
        roles = {c: p.role.title() for c, p in self.parties.items()}
        self.contract_pdf = render(self.contract, roles, self.data_dir / "contracts")
        self._log("contract_pdf", contract_id=self.contract.contract_id, path=str(self.contract_pdf))
        self.t.post(COORDINATOR, json.dumps({"kind": "contract_pdf", "contract_id": self.contract.contract_id,
                                             "path": str(self.contract_pdf)}))

    def approve(self, company_id: str, role: str, approver: str, comment: str = ""):
        self._decide(company_id, role, approver, "approved", comment)

    def reject(self, company_id: str, role: str, approver: str, comment: str = ""):
        self._decide(company_id, role, approver, "rejected", comment)
