import asyncio
import pathlib
import tempfile

import pytest

from negotiation.agent import ScriptedBrain
from negotiation.config import load_private, load_public
from negotiation.fallback import next_vendor, run_with_fallback
from negotiation.models import Proposal, ProposalType, Status
from negotiation.outreach import ConsentPolicy, OutreachDesk
from negotiation.registry import Registry

NEED = "need-retailcorp-analytics"


class Quitter(ScriptedBrain):
    def decide(self, incoming, feedback=None):
        return Proposal(type=ProposalType.walk_away, rationale="Not a fit.")


def quitting_buyer():
    return Quitter(load_public("company_b"), load_private("company_b"))


def test_falls_back_to_next_ranked_vendor(tmp_path):
    attempts, coord, _ = run_with_fallback(NEED, brains={"company_a": {"company_b": quitting_buyer()}},
                                           data_dir=tmp_path)
    assert [(a.vendor_id, a.status) for a in attempts] == [("company_a", "IMPASSE"),
                                                            ("vendor_retailiq", "PENDING_APPROVAL")]
    assert coord.parties["vendor_retailiq"].name == "RetailIQ" and coord.contract


def test_no_fallback_when_first_vendor_agrees(tmp_path):
    attempts, coord, _ = run_with_fallback(NEED, data_dir=tmp_path)
    assert [a.vendor_id for a in attempts] == ["company_a"] and coord.status == Status.pending_approval


def test_exhausted_when_every_vendor_fails(tmp_path):
    q = {"company_b": quitting_buyer()}
    attempts, coord, step = run_with_fallback(NEED, brains={"company_a": q, "vendor_retailiq": {"company_b": quitting_buyer()}},
                                              data_dir=tmp_path)
    assert coord is None and step.exhausted and len(attempts) == 2


def test_fallback_respects_consent():
    reg = Registry.load()
    desk = OutreachDesk(reg, {"company_b": ConsentPolicy(blocked_vendors=["vendor_retailiq"])})
    step = next_vendor(reg, desk, NEED, tried=["company_a"])
    assert step.exhausted and any("declined" in n for n in step.notes)


def test_fallback_pauses_for_human_consent():
    reg = Registry.load()
    desk = OutreachDesk(reg, {"company_b": ConsentPolicy(min_match_score=0.99, human_review_band=0.1)})
    step = next_vendor(reg, desk, NEED, tried=["company_a"])
    assert step.waiting and step.waiting.vendor_id == "vendor_retailiq" and not step.vendor_id


def test_fallback_skips_vendor_without_agent():
    reg = Registry.load()
    step = next_vendor(reg, OutreachDesk(reg), NEED, tried=["company_a"], available=lambda v: False)
    assert step.exhausted and any("no negotiating agent" in n for n in step.notes)


# ---------- over the simulated Band room ----------
pytest.importorskip("band")
from negotiation.band_runtime import BAND_NAMES, CompanyAdapter, CoordinatorAdapter  # noqa: E402
from tests.test_band_adapters import Room  # noqa: E402


def band_room():
    room = Room()
    room.adapters[BAND_NAMES["coordinator"]] = CoordinatorAdapter(
        data_dir=pathlib.Path(tempfile.mkdtemp()), sellers_online={"company_a", "vendor_retailiq", "company_b"})
    room.adapters[BAND_NAMES["vendor_retailiq"]] = CompanyAdapter("vendor_retailiq", "scripted")
    return room


def test_band_walkaway_triggers_fallback():
    room = band_room()
    coord_ad = room.adapters[BAND_NAMES["coordinator"]]
    # Stop CloudCo's turn mid-negotiation by having the buyer's human walk away right after start.
    room.adapters.pop(BAND_NAMES["company_a"])
    asyncio.run(room.human("start"))
    asyncio.run(room.human("walkaway price too high"))
    coord = coord_ad.rooms["room1"][0]
    assert coord.parties.get("vendor_retailiq") and coord.status == Status.pending_approval
    texts = " ".join(c for _, _, c in room.timeline)
    assert "Falling back to **RetailIQ**" in texts and "IMPASSE" in texts
    assert coord_ad.tried["room1"] == ["company_a", "vendor_retailiq"]


def test_band_reports_when_no_vendors_left():
    room = band_room()
    room.adapters.pop(BAND_NAMES["company_a"])
    room.adapters.pop(BAND_NAMES["vendor_retailiq"])
    asyncio.run(room.human("start"))
    asyncio.run(room.human("walkaway"))
    asyncio.run(room.human("walkaway"))
    assert "No more eligible vendors" in room.timeline[-1][2]
