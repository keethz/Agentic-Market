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
from .config import CONFIG, has_agent, load_private, load_public
from .fallback import next_vendor
from .coordinator import COORDINATOR, Coordinator
from .models import Status
from .policy_service import PolicyService
from .matchmaker import opportunities_for, rank
from .outreach import OutreachDesk
from .registry import Registry
from .system import BRAINS
from .transport import Message, Transport

log = logging.getLogger("negotiation.band")

BUYER = "company_b"
SELLERS = ("company_a", "vendor_retailiq")
COMPANIES = SELLERS + (BUYER,)
BAND_NAMES = {"company_a": "CloudCo Sales", "company_b": "RetailCorp Procurement",
              "vendor_retailiq": "RetailIQ Sales", "coordinator": "Deal Coordinator"}
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
    def __init__(self, max_rounds: int = 16, data_dir=None, sellers_online=None):
        super().__init__()
        self.max_rounds, self.data_dir = max_rounds, data_dir
        self.rooms: Dict[str, Tuple[Coordinator, _Outbox, str]] = {}  # room -> (coord, outbox, human)
        self.tried: Dict[str, List[str]] = {}      # room -> vendors already negotiated with
        self.waiting: Dict[str, str] = {}          # room -> outreach request awaiting human consent
        self.handled_impasse: Dict[str, str] = {}  # room -> negotiation_id already fallen back from
        self.sellers_online = set(sellers_online) if sellers_online is not None else _configured_agents()
        self.policy = PolicyService({c: load_private(c) for c in COMPANIES if has_agent(c)})  # stands in for policy_api
        self.registry = Registry.load()
        self.desk = OutreachDesk(self.registry)
        self.default_need = "need-retailcorp-analytics"

    async def _flush(self, tools, outbox: _Outbox, human: str):
        items, outbox.items = outbox.items, []
        for recipient, content in items:
            payload = json.loads(content)
            if payload.get("kind") == "contract_pdf":
                await _send_pdf(tools, payload, human)
                continue
            to = BAND_NAMES[recipient] if recipient else human
            await tools.send_message(render(payload), mentions=[to])

    async def _ensure_participants(self, tools, ids=(SELLERS[0], BUYER)):
        present = {p.get("name") for p in tools.participants} if tools.participants else set()
        for cid in ids:
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
            await self._flush(tools, outbox, human)
            return await self._maybe_fallback(tools, room_id, human)

        if msg.sender_type == "Agent":
            return await tools.no_reply("ignoring unknown agent")

        # Human commands
        human = msg.sender_name or "there"
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower()
        if cmd in ("needs", "matches", "opportunities", "outreach", "consent"):
            await tools.send_message(self._marketplace(cmd, rest.strip(), human), mentions=[human])
            req_id = rest.split()[0] if rest.split() else None
            if cmd == "consent" and req_id and self.waiting.get(room_id) == req_id:
                r = self.desk.requests[self.waiting.pop(room_id)]
                if r.decision == "accepted":
                    return await self._open(tools, room_id, human, r.vendor_id)
                return await self._fallback(tools, room_id, human)
            return
        if cmd == "start":
            if state and state[0].status in (Status.negotiating, Status.pending_approval):
                return await tools.send_message(f"A negotiation is already {state[0].status.value}.", mentions=[human])
            self.tried[room_id] = []
            self.waiting.pop(room_id, None)
            return await self._next(tools, room_id, human, first=True)
        if cmd in ("walkaway", "walk-away", "end"):
            if not state or state[0].status != Status.negotiating:
                return await tools.send_message("No negotiation is open.", mentions=[human])
            coord, outbox, _ = state
            buyer_name = coord.parties[BUYER].name
            coord.end(BUYER, f"{buyer_name} ({human}) walked away" + (f": {rest.strip()}" if rest.strip() else ""))
            await self._flush(tools, outbox, human)
            return await self._maybe_fallback(tools, room_id, human)
        if not state and cmd == "analytics":
            from . import analytics
            from .coordinator import DATA
            return await tools.send_message("📈 **Portfolio analytics (all deals)**\n" + analytics.summary_report(
                analytics.summarize(self.data_dir or DATA)), mentions=[human])
        if not state:
            return await tools.send_message("Say `@Deal Coordinator start` to open a negotiation.", mentions=[human])
        coord, outbox, _ = state
        if cmd == "contract":
            if not coord.contract_pdf:
                return await tools.send_message("The PDF is generated once the contract is FINALIZED by all approvers.", mentions=[human])
            return await _send_pdf(tools, {"contract_id": coord.contract.contract_id, "path": str(coord.contract_pdf)}, human)
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
        if cmd == "analytics":
            from . import analytics
            from .coordinator import DATA
            body = f"📈 **Portfolio analytics (all deals)**\n{analytics.summary_report(analytics.summarize(self.data_dir or DATA))}"
            if coord.analytics:
                body = f"📊 **This deal**\n{analytics.report(coord.analytics)}\n\n" + body
            return await tools.send_message(body, mentions=[human])
        return await tools.send_message("Commands: `needs`, `matches`, `opportunities`, `outreach <vendor>`, `consent <id> yes|no`, `start`, `walkaway [reason]`, `status`, `contract`, `analytics`, `approve <role>`, `reject <role> <reason>`.", mentions=[human])

    # ---------- vendor fallback ----------
    async def _open(self, tools, room_id, human, vendor_id):
        """Open a negotiation between the buyer and vendor_id (consent already granted)."""
        self.tried.setdefault(room_id, []).append(vendor_id)
        await self._ensure_participants(tools, (vendor_id, BUYER))
        outbox = _Outbox()
        coord = Coordinator(outbox, self.policy, load_public(vendor_id), load_public(BUYER),
                            max_rounds=self.max_rounds, **({"data_dir": self.data_dir} if self.data_dir else {}))
        self.rooms[room_id] = (coord, outbox, human)
        coord.start()
        await self._flush(tools, outbox, human)

    async def _next(self, tools, room_id, human, first=False):
        step = next_vendor(self.registry, self.desk, self.default_need, self.tried.get(room_id, []),
                           available=lambda v: v in self.sellers_online and has_agent(v))
        if step.notes:
            await tools.send_message("\n".join(step.notes), mentions=[human])
        if step.vendor_id:
            if not first:
                await tools.send_message(f"🔁 Falling back to **{self.registry.vendors[step.vendor_id].name}**, "
                                         "the next-ranked vendor with buyer consent.", mentions=[human])
            return await self._open(tools, room_id, human, step.vendor_id)
        if step.waiting:
            self.waiting[room_id] = step.waiting.request_id
            return await tools.send_message(
                f"⏸ Waiting for the buyer to review outreach from {self.registry.vendors[step.waiting.vendor_id].name}: "
                f"`@Deal Coordinator consent {step.waiting.request_id} yes|no`", mentions=[human])
        tried = ", ".join(self.registry.vendors[v].name for v in self.tried.get(room_id, [])) or "none"
        return await tools.send_message(f"🛑 No more eligible vendors with buyer consent (tried: {tried}). "
                                        "The need stays open.", mentions=[human])

    async def _fallback(self, tools, room_id, human):
        return await self._next(tools, room_id, human)

    async def _maybe_fallback(self, tools, room_id, human):
        state = self.rooms.get(room_id)
        if not state or state[0].status != Status.impasse:
            return
        if self.handled_impasse.get(room_id) == state[0].negotiation_id:
            return
        self.handled_impasse[room_id] = state[0].negotiation_id
        return await self._fallback(tools, room_id, human)

    def _vendor(self, text: str):
        t = text.lower()
        return next((v.vendor_id for v in self.registry.vendors.values()
                     if t in (v.vendor_id.lower(), v.name.lower()) or v.name.lower().startswith(t)), None) if t else None

    def _marketplace(self, cmd: str, arg: str, human: str) -> str:
        reg, need = self.registry, arg if arg in self.registry.needs else self.default_need
        if cmd == "needs":
            return "📋 **Open buyer needs** (public; true budgets stay private)\n" + "\n".join(
                f"- `{n.need_id}` {n.buyer}: {n.summary} | must: {', '.join(n.must_have.capabilities + n.must_have.integrations)}"
                f" | {n.seats.get('min')}–{n.seats.get('max')} seats | budget band {n.budget_band} | intent {n.buying_intent}"
                for n in reg.open_needs())
        if cmd == "matches":
            return f"🔎 **Ranked vendors for `{need}`**\n" + "\n\n".join(
                m.explain() for m in rank(reg, need, include_ineligible=True))
        if cmd == "opportunities":
            vid = self._vendor(arg) or "company_a"
            ms = opportunities_for(reg, vid)
            return f"🎯 **Opportunities for {reg.vendors[vid].name}**\n" + ("\n\n".join(m.explain() for m in ms) or "none")
        if cmd == "outreach":
            vid = self._vendor(arg.split()[0] if arg else "")
            if not vid:
                return "Usage: `outreach <vendor>` e.g. `outreach CloudCo`."
            try:
                return self.desk.public_view(self.desk.request(vid, self.default_need, f"requested by {human}"))
            except ValueError as e:
                return f"Outreach refused: {e}"
        if cmd == "consent":
            parts = arg.split()
            if len(parts) < 2 or parts[0] not in self.desk.requests:
                return "Usage: `consent <request_id> yes|no`."
            try:
                return self.desk.public_view(self.desk.decide(parts[0], parts[1].lower() in ("yes", "y", "accept"), human))
            except ValueError as e:
                return str(e)
        return ""


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
    outbox.items = [i for i in outbox.items
                    if json.loads(i[1]).get("kind") not in ("status", "contract_pdf")]  # summarised below
    await tools.send_message("\n".join(lines) or "No approve/reject command found.", mentions=[human])
    if coord.status == Status.finalized:
        c = coord.contract
        await tools.send_message(f"🏁 Contract **{c.contract_id}** is FINALIZED and recorded "
                                 f"(hash `{c.terms_hash[:12]}`, total ${c.total_value:,.0f}).", mentions=[human])
        if coord.contract_pdf:
            await _send_pdf(tools, {"contract_id": c.contract_id, "path": str(coord.contract_pdf)}, human)
    elif coord.status == Status.rejected:
        await tools.send_message("❌ Contract rejected. Say `@Deal Coordinator start` to renegotiate.", mentions=[human])
    await self._flush(tools, outbox, human)  # analytics


async def _send_pdf(tools, payload, human):
    """Upload the contract PDF into the room; fall back to a text notice."""
    import hashlib
    from pathlib import Path
    path = Path(payload["path"])
    caption = f"📄 Signed-off contract **{payload['contract_id']}** (PDF)"
    try:
        body = path.read_bytes()
        up = await tools.rest.agent_api_files.upload_agent_chat_file(
            chat_id=tools.room_id, request=body,
            request_options={"additional_headers": {"x-file-name": path.name,
                                                    "x-file-sha256": hashlib.sha256(body).hexdigest(),
                                                    "content-type": "application/pdf"}})
        return await tools.send_message(caption, mentions=[human], attachment_ids=[up.data.id])
    except Exception as e:
        log.warning("PDF upload failed: %s", e)
        return await tools.send_message(f"{caption} saved at `data/contracts/{path.name}`.", mentions=[human])


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


def _configured_agents() -> set:
    """Company agents that have Band credentials in agent_config.yaml."""
    import yaml
    p = CONFIG.parent / "agent_config.yaml"
    keys = set((yaml.safe_load(p.read_text()) or {}).keys()) if p.exists() else set()
    return {c for c in COMPANIES if c in keys}


async def run(brain: Optional[str] = None):
    brain = brain or os.environ.get("AGENT_BRAIN", "gemini")
    online = _configured_agents()
    agents = [Agent.from_config("coordinator", adapter=CoordinatorAdapter(sellers_online=online))]
    agents += [Agent.from_config(c, adapter=CompanyAdapter(c, brain)) for c in COMPANIES if c in online]
    log.info("Starting Deal Coordinator + %s (brain=%s)", ", ".join(BAND_NAMES[c] for c in COMPANIES if c in online), brain)
    async with contextlib.AsyncExitStack() as stack:
        running = [await stack.enter_async_context(a) for a in agents]
        await asyncio.gather(*(a.run_forever() for a in running))
