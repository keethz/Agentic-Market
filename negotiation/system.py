"""Wire up coordinator + two agents on a transport."""
from __future__ import annotations

from .agent import ClaudeBrain, GeminiBrain, NegotiationAgent, ScriptedBrain

BRAINS = {"claude": ClaudeBrain, "gemini": GeminiBrain, "scripted": ScriptedBrain}
from .config import load_private, load_public
from .coordinator import Coordinator
from .policy_service import PolicyService
from .transport import LocalTransport, Transport


def build(brain: str = "scripted", transport: Transport = None, max_rounds: int = 16, **kw):
    t = transport or LocalTransport()
    a_pub, b_pub = load_public("company_a"), load_public("company_b")
    a_pol, b_pol = load_private("company_a"), load_private("company_b")
    # Policy Service holds both policies; each agent runtime holds only its own.
    coord = Coordinator(t, PolicyService({"company_a": a_pol, "company_b": b_pol}), a_pub, b_pub,
                        max_rounds=max_rounds, **kw)
    mk = BRAINS[brain]
    agent_a = NegotiationAgent(a_pub, a_pol, mk(a_pub, a_pol), t)
    agent_b = NegotiationAgent(b_pub, b_pol, mk(b_pub, b_pol), t)
    return t, coord, agent_a, agent_b
