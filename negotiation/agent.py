"""Company negotiation agents. Each runs in its own private runtime and only
talks to the coordinator. Its secrets live in its system prompt, never in messages."""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from .guardrails import leak_findings, self_check
from .models import (SUPPORT_TIERS, TERM_SCHEMA, PrivatePolicy, Proposal, ProposalType,
                     PublicProfile, normalize_terms, total_value)
from .transport import Message, Transport

COORDINATOR = "coordinator"

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": [t.value for t in ProposalType]},
        "terms": {
            "type": "object",
            "properties": {
                "unit_price": {"type": "number"},
                "quantity": {"type": "integer"},
                "contract_months": {"type": "integer"},
                "payment_terms_days": {"type": "integer"},
                "sla_uptime": {"type": "number"},
                "support_tier": {"type": "string", "enum": SUPPORT_TIERS},
            },
            "required": list(TERM_SCHEMA),
            "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": ["type", "terms", "rationale"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- brains
class Brain:
    """Decides the next proposal given the incoming coordinator message."""
    def decide(self, incoming: dict, feedback: Optional[str] = None) -> Proposal: ...


class ClaudeBrain(Brain):
    def __init__(self, me: PublicProfile, policy: PrivatePolicy, model: Optional[str] = None):
        import anthropic  # lazy so tests run without the SDK
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("AGENT_MODEL", "claude-opus-5-5")
        self.system = self._system_prompt(me, policy)
        self.messages: List[dict] = []  # append-only history

    @staticmethod
    def _system_prompt(me: PublicProfile, policy: PrivatePolicy) -> str:
        goal = ("maximise revenue and contract value" if me.role == "seller"
                else "minimise total cost while meeting requirements")
        limits = "\n".join(f"- {k}: {v.model_dump(exclude_none=True)}" for k, v in policy.terms.items())
        nn = "\n".join(f"- {k}: {v.model_dump(exclude_none=True)}" for k, v in policy.non_negotiable.items()) or "- none"
        return f"""You are the {'Sales' if me.role == 'seller' else 'Procurement'} agent for {me.name}.
Public profile: {me.model_dump_json(exclude_none=True)}
Your goal: {goal}.

CONFIDENTIAL (known only to you; never reveal, hint at, or restate these):
Objectives: {policy.objectives}
Acceptable ranges per term (min/max/target):
{limits}
Non-negotiable:
{nn}

How negotiation works:
- You talk only to a neutral coordinator, which relays your proposals to the counterparty.
- Every message you send is visible to human observers of the room. Your rationale must use
  public business reasons only (volume, market rates, rollout scope, service quality).
  Never mention your limits, targets, budget, approval rules, or words like minimum/maximum/floor/ceiling.
- Open near your target and concede gradually, trading terms (e.g. longer contract for lower price).
- Never propose or accept terms outside your acceptable ranges.
- "offer"/"counter": propose a full term sheet. "accept": accept the counterparty's latest terms
  exactly as given (copy them). If you receive a confirm_request for terms you proposed and still
  want, reply "accept" with identical terms. "walk_away" only if agreement is clearly impossible.
- total_value = unit_price * quantity * contract_months / 12.
- The negotiation ends with NO DEAL when rounds_left reaches 0 — a no-deal is a bad outcome for you too.
  Never resend the same terms twice in a row. If the counterparty holds firm on a term for two rounds,
  concede on it (within your ranges) or trade it for something you value more.
- If the counterparty's latest terms are within all your acceptable ranges and rounds_left is small,
  prefer "accept" over another counter.
Always respond with a single proposal JSON."""

    def decide(self, incoming, feedback=None):
        text = json.dumps(incoming) if feedback is None else f"Your last proposal was blocked: {feedback}. Send a corrected proposal."
        self.messages.append({"role": "user", "content": text})
        resp = self.client.messages.create(
            model=self.model, max_tokens=16000, system=self.system, messages=self.messages,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}},
        )
        self.messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "refusal":
            return Proposal(type=ProposalType.walk_away, rationale="Unable to continue.")
        out = next(b.text for b in resp.content if b.type == "text")
        return Proposal(**json.loads(out))


class GeminiBrain(Brain):
    """Company agent backed by Google Gemini (free tier friendly)."""
    FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

    def __init__(self, me: PublicProfile, policy: PrivatePolicy, model: Optional[str] = None):
        from google import genai  # lazy so tests run without the SDK
        from google.genai import types
        # Fail fast (no long SDK retry loops) so we can fall back to another free model.
        self.client = genai.Client(
            api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"),
            http_options=types.HttpOptions(timeout=60000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        self.system = ClaudeBrain._system_prompt(me, policy)
        self.contents: List[dict] = []

    def _call(self, model: str):
        return self.client.models.generate_content(
            model=model, contents=self.contents,
            config={"system_instruction": self.system, "response_mime_type": "application/json",
                    "response_json_schema": PROPOSAL_SCHEMA, "temperature": 0.4})

    def decide(self, incoming, feedback=None):
        import time
        text = json.dumps(incoming) if feedback is None else f"Your last proposal was blocked: {feedback}. Send a corrected proposal."
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        last_err = None
        models = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        for rnd in range(3):  # up to 3 sweeps over the models
            for model in models:
                try:
                    out = self._call(model).text
                    self.contents.append({"role": "model", "parts": [{"text": out}]})
                    self.model = model  # stick with what works
                    return Proposal(**json.loads(out))
                except Exception as e:  # 503 overload / 429 rate limit -> next model
                    last_err = e
            time.sleep(5 * (rnd + 1))
        raise RuntimeError(f"Gemini unavailable: {last_err}")


class ScriptedBrain(Brain):
    """Deterministic negotiator for offline demos and tests: starts at its target and
    concedes toward the counterparty's last offer, clamped to its own ranges."""
    def __init__(self, me: PublicProfile, policy: PrivatePolicy, concede_rounds: int = 5):
        self.me, self.p, self.n = me, policy, concede_rounds
        self.turn = 0

    def _target(self) -> Dict:
        t = {}
        for k, b in self.p.terms.items():
            t[k] = b.allowed[-1] if b.allowed else b.target
        return normalize_terms(t)

    def _clamp(self, k, v):
        b = self.p.terms[k]
        if b.allowed:
            return v if v in b.allowed else b.allowed[-1]
        return min(max(v, b.min), b.max)

    def decide(self, incoming, feedback=None):
        self.turn += 1
        kind, theirs = incoming.get("kind"), incoming.get("terms")
        if kind == "confirm_request":
            if not self_check(self.p, theirs):
                return Proposal(type=ProposalType.accept, terms=theirs, rationale="Confirmed.")
        frac = min(1.0, self.turn / self.n)
        if theirs and not self_check(self.p, theirs) and frac >= 0.6:
            return Proposal(type=ProposalType.accept, terms=theirs, rationale="These terms work for us.")
        mine = self._target()
        if theirs:
            for k, kind_ in TERM_SCHEMA.items():
                if kind_ == "enum":
                    mine[k] = self._clamp(k, theirs[k])
                else:
                    mine[k] = self._clamp(k, mine[k] + (float(theirs[k]) - mine[k]) * frac)
        mine = normalize_terms(mine)
        why = ("Reflects the platform's value at this rollout scale." if self.me.role == "seller"
               else "Aligned with market rates for a deployment of this size.")
        return Proposal(type=ProposalType.counter if theirs else ProposalType.offer, terms=mine, rationale=why)


# ---------------------------------------------------------------- agent
class NegotiationAgent:
    MAX_RETRIES = 2

    def __init__(self, me: PublicProfile, policy: PrivatePolicy, brain: Brain, transport: Optional[Transport] = None):
        self.me, self.policy, self.brain, self.t = me, policy, brain, transport
        self.log: List[str] = []
        if transport is not None:
            transport.subscribe(me.id, self.on_message)

    def on_message(self, m: Message):
        if m.sender != COORDINATOR:
            return
        p = self.respond(json.loads(m.content))
        if p is not None:
            self.t.dm(self.me.id, COORDINATOR, p.model_dump_json())

    def respond(self, incoming: dict) -> Optional[Proposal]:
        """Decide the next guarded proposal for a coordinator message (transport-agnostic)."""
        if not incoming.get("your_turn"):
            return None
        feedback = None
        if incoming.get("kind") == "error":
            feedback = f"coordinator rejected it: {incoming['codes']}"
        for _ in range(self.MAX_RETRIES + 1):
            p = self.brain.decide(incoming, feedback)
            problems = self._guard(p)
            if not problems:
                return p
            self.log.append(f"blocked: {problems}")
            feedback = "; ".join(problems)
        # Could not produce a safe proposal: walk away rather than leak or breach.
        return Proposal(type=ProposalType.walk_away, rationale="We are unable to continue.")

    def _guard(self, p: Proposal) -> List[str]:
        issues = []
        if p.terms is not None and p.type != ProposalType.walk_away:
            issues += [f"own policy: {c}" for c in self_check(self.policy, p.terms)]
        leaks = leak_findings(p.rationale, self.policy, p.terms)
        if leaks:
            issues.append(f"rationale may reveal confidential info ({', '.join(leaks)}); use public reasons only")
        return issues
