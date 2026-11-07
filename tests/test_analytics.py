import json

from negotiation import analytics
from negotiation.agent import NegotiationAgent
from negotiation.config import load_private, load_public
from negotiation.models import Proposal, ProposalType, Status
from negotiation.system import build
from tests.test_negotiation import GOOD, Harness


def test_metrics_after_scripted_deal(tmp_path):
    t, coord, *_ = build("scripted", data_dir=tmp_path)
    coord.start()
    t.run_until_idle()
    m = coord.analytics
    assert m["outcome"] == "PENDING_APPROVAL" and m["agreement_reached"]
    assert m["rounds"] == coord.round
    assert m["seller_initial_price"] == 115.0 and m["buyer_initial_price"] < m["final_price"] < 115.0
    assert m["seller_discount_pct"] > 0 and m["buyer_savings"] > 0
    assert m["rejected_offers"] == m["rounds"] - 1
    assert m["time_to_agreement_s"] is not None and not m["walked_away"]
    assert (tmp_path / "analytics" / f"{coord.negotiation_id}.json").exists()
    # posted to the room
    assert any(json.loads(x.content).get("kind") == "analytics" for x in t.timeline)
    # recomputed when humans finalize
    for a in coord.contract.approvals:
        coord.approve(a.company_id, a.role, "t")
    assert coord.analytics["outcome"] == "FINALIZED"


def test_policy_violations_and_walk_away_counted(tmp_path):
    h = Harness(tmp_path)
    h.send("company_a", "offer", dict(GOOD, unit_price=50))          # breaks own policy
    h.t.dm("company_a", "coordinator", json.dumps(
        {"type": "walk_away", "rationale": "x", "meta": {"policy_blocks": 2, "leak_blocks": 3, "walk_aways": 1}}))
    h.t.run_until_idle()
    m = h.c.analytics
    assert m["outcome"] == "IMPASSE" and m["walked_away"]
    assert m["policy_violations"] == 1 + 2
    assert m["secret_leak_attempts"] == 3
    assert m["coordinator_rejections"] == 1


def test_agent_guardrail_counters():
    me, pol = load_public("company_b"), load_private("company_b")

    class Leaky:
        n = 0
        def decide(self, incoming, feedback=None):
            self.n += 1
            if self.n == 1:
                return Proposal(type=ProposalType.offer, terms=dict(GOOD, unit_price=150), rationale="ok")
            if self.n == 2:
                return Proposal(type=ProposalType.offer, terms=GOOD, rationale="our budget is 100")
            return Proposal(type=ProposalType.offer, terms=GOOD, rationale="market rate")

    p = NegotiationAgent(me, pol, Leaky()).respond({"kind": "proposal", "your_turn": True})
    assert p.meta == {"policy_blocks": 1, "leak_blocks": 1, "walk_aways": 0}
    assert "budget" not in p.rationale


def test_portfolio_summary(tmp_path):
    analytics.save(tmp_path, "n1", {"outcome": "FINALIZED", "agreement_reached": True, "rounds": 6,
                                     "time_to_agreement_s": 20, "seller_discount_pct": 10, "buyer_savings": 1000,
                                     "final_total": 50000, "policy_violations": 1, "secret_leak_attempts": 0,
                                     "walked_away": False})
    analytics.save(tmp_path, "n2", {"outcome": "IMPASSE", "agreement_reached": False, "rounds": 16,
                                     "policy_violations": 0, "secret_leak_attempts": 2, "walked_away": True})
    s = analytics.summarize(tmp_path)
    assert s["negotiations"] == 2 and s["agreement_rate_pct"] == 50.0
    assert s["walk_away_rate_pct"] == 50.0 and s["avg_rounds"] == 11.0
    assert s["total_contract_value"] == 50000 and s["total_secret_leak_attempts"] == 2
    assert "Walk-away rate: 50.0%" in analytics.summary_report(s)


def test_analytics_contains_no_secrets(tmp_path):
    t, coord, *_ = build("scripted", data_dir=tmp_path)
    coord.start()
    t.run_until_idle()
    text = json.dumps(coord.analytics)
    for word in ("budget", "minimum", "maximum", "objective", "target"):
        assert word not in text
