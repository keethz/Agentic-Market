import json

import pytest

from negotiation.config import load_all, load_private
from negotiation.coordinator import Coordinator
from negotiation.guardrails import leak_findings
from negotiation.leak_scan import scan
from negotiation.models import Status
from negotiation.policy_service import PolicyService
from negotiation.system import build
from negotiation.transport import LocalTransport

GOOD = {"unit_price": 95, "quantity": 450, "contract_months": 18,
        "payment_terms_days": 40, "sla_uptime": 99.92, "support_tier": "premium"}


@pytest.fixture
def setup(tmp_path):
    t, coord, a, b = build("scripted", data_dir=tmp_path)
    return t, coord


def run(setup):
    t, coord = setup
    coord.start()
    t.run_until_idle()
    return t, coord


def test_full_flow_reaches_pending_then_finalized(setup):
    t, coord = run(setup)
    assert coord.status == Status.pending_approval
    for a in coord.contract.approvals:
        coord.approve(a.company_id, a.role, "tester")
    assert coord.status == Status.finalized
    assert coord.contract.terms_hash


def test_room_timeline_has_no_secrets(setup):
    t, _ = run(setup)
    assert scan(t.timeline, load_all()) == []


def test_not_finalized_without_all_approvals(setup):
    t, coord = run(setup)
    first = coord.contract.approvals[0]
    coord.approve(first.company_id, first.role, "tester")
    assert coord.status == Status.pending_approval


def test_rejection_blocks_contract(setup):
    t, coord = run(setup)
    a = coord.contract.approvals[0]
    coord.reject(a.company_id, a.role, "tester", "too expensive")
    assert coord.status == Status.rejected


def test_policy_returns_codes_not_numbers():
    svc = PolicyService(load_all())
    r = svc.check("company_b", dict(GOOD, unit_price=150))
    assert not r.ok and r.codes == ["OUT_OF_RANGE"]
    assert "100" not in r.model_dump_json()


def test_non_negotiable():
    svc = PolicyService(load_all())
    assert "NON_NEGOTIABLE_VIOLATED" in svc.check("company_b", dict(GOOD, support_tier="standard")).codes


class Harness:
    """Drive the coordinator directly with hand-written agent messages."""
    def __init__(self, tmp_path):
        self.t = LocalTransport()
        from negotiation.config import load_public
        self.c = Coordinator(self.t, PolicyService(load_all()), load_public("company_a"),
                             load_public("company_b"), data_dir=tmp_path)
        self.inbox = {"company_a": [], "company_b": []}
        for k in self.inbox:
            self.t.subscribe(k, lambda m, k=k: self.inbox[k].append(json.loads(m.content)))
        self.c.start()
        self.t.run_until_idle()

    def send(self, who, type_, terms=None, rationale="ok"):
        self.t.dm(who, "coordinator", json.dumps({"type": type_, "terms": terms, "rationale": rationale}))
        self.t.run_until_idle()
        return self.inbox[who][-1] if self.inbox[who] else None


def test_turn_order_enforced(tmp_path):
    h = Harness(tmp_path)
    assert h.send("company_b", "offer", GOOD)["codes"] == ["NOT_YOUR_TURN"]


def test_near_identical_accept_is_not_agreement(tmp_path):
    h = Harness(tmp_path)
    h.send("company_a", "offer", GOOD)
    reply = h.send("company_b", "accept", dict(GOOD, unit_price=94.99))
    assert reply["codes"] == ["ACCEPT_TERMS_MISMATCH"]
    assert h.c.status == Status.negotiating


def test_single_accept_needs_confirmation(tmp_path):
    h = Harness(tmp_path)
    h.send("company_a", "offer", GOOD)
    h.send("company_b", "accept", GOOD)
    assert h.c.status == Status.negotiating
    assert h.inbox["company_a"][-1]["kind"] == "confirm_request"
    h.send("company_a", "accept", GOOD)
    assert h.c.status == Status.pending_approval


def test_sender_cannot_breach_own_limits(tmp_path):
    h = Harness(tmp_path)
    assert h.send("company_a", "offer", dict(GOOD, unit_price=50))["codes"] == ["OUT_OF_RANGE"]


def test_offer_outside_receiver_range_is_forwarded_without_leak(tmp_path):
    h = Harness(tmp_path)
    h.send("company_a", "offer", dict(GOOD, unit_price=130))   # above B's budget
    assert h.inbox["company_b"][-1]["kind"] == "proposal"
    assert not h.inbox["company_a"] or h.inbox["company_a"][-1]["kind"] == "start"


def test_walk_away_is_impasse(tmp_path):
    h = Harness(tmp_path)
    h.send("company_a", "walk_away")
    assert h.c.status == Status.impasse


def test_guardrail_catches_leaky_rationale():
    pol = load_private("company_b")
    assert leak_findings("Our budget is 100 per seat", pol)
    assert leak_findings("We can go to 100", pol)          # secret max, not offered
    assert not leak_findings("We can do 100", pol, {"unit_price": 100})  # already offered
