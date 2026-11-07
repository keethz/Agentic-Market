"""Run the negotiation on Band: three Band agents, each a small custom adapter.

- Deal Coordinator: deterministic Coordinator. Humans talk to it with
  `@Deal Coordinator start | status | approve <role> | reject <role> <reason>`.
- CloudCo Sales / RetailCorp Procurement: NegotiationAgent (Gemini/Claude brain +
  guardrails). They only act on messages from the coordinator.

Band delivers a message to an agent only when it is @mentioned, so each company
sees only what the coordinator relays. Humans in the room see everything, which
is why secrets never leave each agent's process / the Policy Service.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import logging
import os
from typing import Dict, List, Optional, Tuple

from band import Agent
from band.core.simple_adapter import SimpleAdapter
from band.runtime.formatters import strip_leading_mentions

from .agent import NegotiationAgent
from .band_format import parse, render
from .config import load_private, load_public
from .coordinator import COORDINATOR, Coordinator
from .models import Status
from .policy_service import PolicyService
from .system import BRAINS
from .transport import Message, Transport

log = logging.getLogger("negotiation.band")

COMPANIES = ("company_a", "company_b")
BAND_NAMES = {"company_a": "CloudCo Sales", "company_b": "RetailCorp Procurement",
              "coordinator": "Deal Coordinator"}
NAME_TO_ID = {v: k for k, v in BAND_NAMES.items()}


class _Outbox(Transport):
    """Collects the Coordinator's dm()/post() calls during one turn so the adapter
    can send them through Band tools afterwards."""
    def __init__(self):
        self.items: List[Tuple[Optional[str], str]] = []

    def subscribe(self, participant, handler):
        pass

    def dm(self, sender, recipient, content):
        self.items.append((recipient, content))

    def post(self, sender, content):
        self.items.append((None, content))


def _sender_id(msg) -> Optional[str]:
    return NAME_TO_ID.get(msg.sender_name or "")


class CoordinatorAdapter(SimpleAdapter):
    def __init__(self, max_rounds: int = 16, data_dir=None):
        super().__init__()
        self.max_rounds, self.data_dir = max_rounds, data_dir
        self.rooms: Dict[str, Tuple[Coordinator, _Outbox, str]] = {}  # room -> (coord, outbox, human)
        self.policy = PolicyService({c: load_private(c) for c in COMPANIES})  # stands in for policy_api

    async def _flush(self, tools, outbox: _Outbox, human: str):
        items, outbox.items = outbox.items, []
        for recipient, content in items:
            payload = json.loads(content)
            to = BAND_NAMES[recipient] if recipient else human
            await tools.send_message(render(payload), mentions=[to])

    async def _ensure_participants(self, tools):
        present = {p.get("name") for p in tools.participants} if tools.participants else set()
        for cid in COMPANIES:
            if BAND_NAMES[cid] not in present:
                try:
                    await tools.add_participant(BAND_NAMES[cid])
                except Exception as e:  # already present / not found
                    log.warning("add_participant %s: %s", BAND_NAMES[cid], e)

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, *,
                         is_session_bootstrap, room_id):
        text = strip_leading_mentions(msg.content).strip()
        # A typed (non-token) "@Deal Coordinator" may be left as "Coordinator ..." or intact.
        text = re.sub(r"^\s*(@?Deal\s+)?Coordinator\b\s*", "", text, flags=re.I)
        sender = _sender_id(msg)
        state = self.rooms.get(room_id)

        if sender in COMPANIES:  # agent proposal
            if not state:
                return await tools.no_reply("no active negotiation")
            coord, outbox, human = state
            coord.on_message(Message(sender=sender, recipient=COORDINATOR,
                                     content=json.dumps(parse(text) or {}), at=""))
            return await self._flush(tools, outbox, human)

        if msg.sender_type == "Agent":
            return await tools.no_reply("ignoring unknown agent")

        # Human commands
        human = msg.sender_name or "there"
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower()
        if cmd == "start":
            await self._ensure_participants(tools)
            pub = [load_public(c) for c in COMPANIES]
            outbox = _Outbox()
            coord = Coordinator(outbox, self.policy, pub[0], pub[1], max_rounds=self.max_rounds,
                                **({"data_dir": self.data_dir} if self.data_dir else {}))
            self.rooms[room_id] = (coord, outbox, human)
            coord.start()
            return await self._flush(tools, outbox, human)
        if not state:
            return await tools.send_message("Say `@Deal Coordinator start` to open a negotiation.", mentions=[human])
        coord, outbox, _ = state
        if cmd == "status":
            c = coord.contract
            extra = ""
            if c:
                extra = "\n" + "\n".join(f"- {coord.parties[a.company_id].name} / {a.role}: "
                                         f"{a.decision or 'pending'}{' by ' + a.approver if a.approver else ''}"
                                         for a in c.approvals)
            return await tools.send_message(f"Status: **{coord.status.value}**, round {coord.round}.{extra}", mentions=[human])
        if cmd in ("approve", "reject"):
            return await self._approvals(tools, coord, outbox, human, text)
        return await tools.send_message("Commands: `start`, `status`, `approve <role>`, `reject <role> <reason>`.", mentions=[human])


_CMD = re.compile(r"\b(approve|reject)\s+(.+?)(?=(?:@\S+\s+)?\b(?:approve|reject)\b|$)", re.I | re.S)


async def _approvals(self, tools, coord, outbox, human, text):
    """Handle one or many `approve <role>` / `reject <role> <reason>` commands in one message.
    Always acknowledges, so approvers see their decision was recorded."""
    if coord.status != Status.pending_approval:
        return await tools.send_message(f"Nothing is awaiting approval (status {coord.status.value}).", mentions=[human])
    lines = []
    for verb, rest in _CMD.findall(text):
        rest = re.sub(r"@\[\[[^\]]*\]\]|@?Deal Coordinator|\bCoordinator\b", "", rest).strip()
        if coord.status != Status.pending_approval:
            break
        slot = next((a for a in coord.contract.approvals if a.decision is None
                     and rest.lower().startswith(a.role.lower())), None)
        if not slot:
            lines.append(f"• `{rest[:30]}` — not a pending role")
            continue
        reason = rest[len(slot.role):].strip()
        getattr(coord, verb.lower())(slot.company_id, slot.role, human, reason)
        lines.append(f"• {coord.parties[slot.company_id].name} / {slot.role}: **{verb.lower()}d** by {human}"
                     + (f" ({reason})" if reason else ""))
    pending = [a.role for a in coord.contract.approvals if a.decision is None]
    if coord.status == Status.pending_approval:
        lines.append(f"Still pending: {', '.join(pending)}")
    outbox.items = [i for i in outbox.items if json.loads(i[1]).get("kind") != "status"]  # summarised below
    await tools.send_message("\n".join(lines) or "No approve/reject command found.", mentions=[human])
    if coord.status == Status.finalized:
        c = coord.contract
        await tools.send_message(f"🏁 Contract **{c.contract_id}** is FINALIZED and recorded "
                                 f"(hash `{c.terms_hash[:12]}`, total ${c.total_value:,.0f}).", mentions=[human])
    elif coord.status == Status.rejected:
        await tools.send_message("❌ Contract rejected. Say `@Deal Coordinator start` to renegotiate.", mentions=[human])


CoordinatorAdapter._approvals = _approvals


class CompanyAdapter(SimpleAdapter):
    def __init__(self, company_id: str, brain: str):
        super().__init__()
        self.cid, self.brain_name = company_id, brain
        self.me, self.policy = load_public(company_id), load_private(company_id)  # own secrets only
        self.agents: Dict[str, NegotiationAgent] = {}

    def _agent(self, room_id: str, fresh: bool) -> NegotiationAgent:
        if fresh or room_id not in self.agents:
            self.agents[room_id] = NegotiationAgent(self.me, self.policy,
                                                    BRAINS[self.brain_name](self.me, self.policy))
        return self.agents[room_id]

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, *,
                         is_session_bootstrap, room_id):
        if msg.sender_name != BAND_NAMES["coordinator"]:
            return await tools.no_reply("only the coordinator may instruct this agent")
        incoming = parse(strip_leading_mentions(msg.content))
        if not incoming:
            return await tools.no_reply("no protocol payload")
        agent = self._agent(room_id, fresh=incoming.get("kind") == "start")
        p = await asyncio.to_thread(agent.respond, incoming)
        if p is None:
            return await tools.no_reply("not my turn")
        await tools.send_message(render(json.loads(p.model_dump_json())),
                                 mentions=[BAND_NAMES["coordinator"]])


async def run(brain: Optional[str] = None):
    brain = brain or os.environ.get("AGENT_BRAIN", "gemini")
    agents = [
        Agent.from_config("coordinator", adapter=CoordinatorAdapter()),
        Agent.from_config("company_a", adapter=CompanyAdapter("company_a", brain)),
        Agent.from_config("company_b", adapter=CompanyAdapter("company_b", brain)),
    ]
    log.info("Starting Deal Coordinator, CloudCo Sales, RetailCorp Procurement (brain=%s)", brain)
    async with contextlib.AsyncExitStack() as stack:
        running = [await stack.enter_async_context(a) for a in agents]
        await asyncio.gather(*(a.run_forever() for a in running))
