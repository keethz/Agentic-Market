"""Wire up coordinator + two agents on a transport."""
from __future__ import annotations

from .agent import ClaudeBrain, GeminiBrain, NegotiationAgent, ScriptedBrain

BRAINS = {"claude": ClaudeBrain, "gemini": GeminiBrain, "scripted": ScriptedBrain}
from .config import load_private, load_public
from .coordinator import Coordinator
from .policy_service import PolicyService
from .transport import LocalTransport, Transport


def build(brain: str = "scripted", transport: Transport = None, max_rounds: int = 16,
          seller: str = "company_a", buyer: str = "company_b", brains=None, **kw):
    """brains: optional {company_id: Brain} overrides (e.g. for tests)."""
    t = transport or LocalTransport()
    a_pub, b_pub = load_public(seller), load_public(buyer)
    a_pol, b_pol = load_private(seller), load_private(buyer)
    # Policy Service holds both policies; each agent runtime holds only its own.
    coord = Coordinator(t, PolicyService({seller: a_pol, buyer: b_pol}), a_pub, b_pub,
                        max_rounds=max_rounds, **kw)
    mk = BRAINS[brain]
    brains = brains or {}
    agent_a = NegotiationAgent(a_pub, a_pol, brains.get(seller) or mk(a_pub, a_pol), t)
    agent_b = NegotiationAgent(b_pub, b_pol, brains.get(buyer) or mk(b_pub, b_pol), t)
    return t, coord, agent_a, agent_b
