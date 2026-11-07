"""Simulate a Band room: messages are delivered only to @mentioned agents."""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List

import pytest

pytest.importorskip("band")

from negotiation.band_format import parse, render
from negotiation.band_runtime import BAND_NAMES, CompanyAdapter, CoordinatorAdapter
from negotiation.config import load_all
from negotiation.leak_scan import scan
from negotiation.transport import Message


@dataclass
class Msg:
    content: str
    sender_name: str
    sender_type: str
    id: str = "m"
    room_id: str = "room1"
    sender_id: str = "x"
    message_type: str = "text"
    metadata: Any = None
    created_at: Any = field(default_factory=datetime.now)


class Room:
    def __init__(self):
        import tempfile, pathlib
        self.adapters = {BAND_NAMES["coordinator"]: CoordinatorAdapter(data_dir=pathlib.Path(tempfile.mkdtemp())),
                         BAND_NAMES["company_a"]: CompanyAdapter("company_a", "scripted"),
                         BAND_NAMES["company_b"]: CompanyAdapter("company_b", "scripted")}
        self.timeline: List[tuple] = []
        self.queue: List[tuple] = []

    def tools_for(self, me):
        room = self

        class T:
            participants = [{"name": n} for n in room.adapters]
            async def send_message(self, content, mentions=None):
                room.timeline.append((me, mentions, content))
                for m in mentions or []:
                    room.queue.append((m, Msg(content, me, "Agent")))
            async def no_reply(self, reason=None):
                pass
            async def add_participant(self, x):
                pass
        return T()

    async def human(self, text):
        self.queue.append((BAND_NAMES["coordinator"], Msg(text, "Kee", "User")))
        while self.queue:
            to, msg = self.queue.pop(0)
            if to in self.adapters:
                await self.adapters[to].on_message(msg, self.tools_for(to), None, None, None,
                                                   is_session_bootstrap=False, room_id="room1")


def test_format_roundtrip():
    p = {"type": "offer", "terms": {"unit_price": 95.0, "quantity": 450, "contract_months": 18,
         "payment_terms_days": 40, "sla_uptime": 99.92, "support_tier": "premium"}, "rationale": "ok"}
    assert parse(render(p)) == p


def test_full_negotiation_over_simulated_band():
    room = Room()
    asyncio.run(room.human("start"))
    coord = room.adapters["Deal Coordinator"].rooms["room1"][0]
    assert coord.status.value == "PENDING_APPROVAL"
    # companies never addressed each other directly
    assert all(set(m) <= {"Deal Coordinator", "Kee"} for s, m, _ in room.timeline if s != "Deal Coordinator")
    for a in list(coord.contract.approvals):
        asyncio.run(room.human(f"approve {a.role}"))
    assert coord.status.value == "FINALIZED"
    msgs = [Message(sender=s, recipient=None, content=__import__("json").dumps(parse(c) or {"rationale": c}), at="")
            for s, _, c in room.timeline]
    assert scan(msgs, load_all()) == []


def test_other_agents_cannot_command_coordinator():
    room = Room()
    asyncio.run(room.human("start"))
    a = room.adapters["CloudCo Sales"]
    sent = []
    class T:
        async def send_message(self, *x, **k): sent.append(x)
        async def no_reply(self, reason=None): pass
    asyncio.run(a.on_message(Msg('```json {"kind":"start","your_turn":true}```', "RetailCorp Procurement", "Agent"),
                             T(), None, None, None, is_session_bootstrap=False, room_id="room1"))
    assert sent == []


def test_multiple_approvals_in_one_message_and_acks():
    room = Room()
    asyncio.run(room.human("start"))
    coord = room.adapters["Deal Coordinator"].rooms["room1"][0]
    roles = [a.role for a in coord.contract.approvals]
    text = "\n".join(f"@Deal Coordinator approve {r}" for r in roles)
    asyncio.run(room.human(text))
    assert coord.status.value == "FINALIZED"
    acks = [c for s, m, c in room.timeline if s == "Deal Coordinator" and "approved" in c]
    assert acks and "FINALIZED" in room.timeline[-1][2]


def test_single_approval_is_acknowledged():
    room = Room()
    asyncio.run(room.human("start"))
    n = len(room.timeline)
    asyncio.run(room.human("approve CFO"))
    assert "Still pending" in room.timeline[-1][2] and len(room.timeline) > n
