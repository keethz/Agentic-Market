import pytest

from negotiation.matchmaker import evaluate, opportunities_for, rank
from negotiation.outreach import ConsentPolicy, OutreachDesk
from negotiation.registry import Registry

NEED = "need-retailcorp-analytics"


@pytest.fixture
def reg():
    return Registry.load()


def test_hard_filters_exclude_and_explain(reg):
    ms = {m.vendor_id: m for m in rank(reg, NEED, include_ineligible=True)}
    assert not ms["vendor_shipfast"].eligible and "category" in " ".join(ms["vendor_shipfast"].gaps)
    assert not ms["vendor_insightly"].eligible and "sap" in " ".join(ms["vendor_insightly"].gaps)
    assert ms["company_a"].eligible and ms["company_a"].reasons


def test_ranking_order_and_scores(reg):
    ms = rank(reg, NEED)
    assert [m.vendor_id for m in ms] == ["company_a", "vendor_retailiq"]
    assert all(0 < m.score <= 1 for m in ms)


def test_custom_semantic_function(reg):
    m = evaluate(reg.needs[NEED], reg.vendors["company_a"], similarity=lambda a, b: 0.0)
    assert m.breakdown["semantic"] == 0.0


def test_seller_opportunities(reg):
    assert [m.need_id for m in opportunities_for(reg, "company_a")] == [NEED]
    assert opportunities_for(reg, "vendor_shipfast") == []


def test_public_need_has_no_true_budget(reg):
    dumped = reg.needs[NEED].model_dump_json()
    assert "100" not in dumped and "max_budget" not in dumped


def test_consent_auto_accept_and_gate(reg):
    d = OutreachDesk(reg, {"company_b": ConsentPolicy(min_match_score=0.6)})
    assert not d.has_consent("company_a", NEED)
    r = d.request("company_a", NEED)
    assert r.decision == "accepted" and d.has_consent("company_a", NEED)
    with pytest.raises(ValueError):
        d.request("company_a", NEED)  # no duplicate spam


def test_ineligible_vendor_cannot_reach_out(reg):
    with pytest.raises(ValueError, match="not eligible"):
        OutreachDesk(reg).request("vendor_shipfast", NEED)


def test_human_review_and_block(reg):
    d = OutreachDesk(reg, {"company_b": ConsentPolicy(min_match_score=0.95, human_review_band=0.5,
                                                      blocked_vendors=["vendor_retailiq"])})
    r = d.request("company_a", NEED)
    assert r.decision == "needs_human"
    assert d.decide(r.request_id, True, "Procurement Lead").decision == "accepted"
    b = d.request("vendor_retailiq", NEED)
    assert b.decision == "declined" and "0.95" not in d.public_view(b) and "blocked" not in d.public_view(b)
