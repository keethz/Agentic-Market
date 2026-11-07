"""Matchmaker: hard structured filters first, then weighted + semantic scoring,
with a human-readable explanation for every recommendation.

Semantic similarity defaults to an offline bag-of-words cosine. Set
MATCH_SEMANTIC=gemini to use Gemini embeddings instead (falls back to offline)."""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from functools import lru_cache
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel

from .registry import BuyerNeed, ProductListing, Registry

SIZES = ["smb", "mid-market", "enterprise"]
INTENT = {"low": 0.3, "medium": 0.6, "high": 1.0}
WEIGHTS = {"capabilities": 0.25, "integrations": 0.15, "semantic": 0.20, "industry": 0.10,
           "size": 0.05, "geography": 0.05, "intent": 0.10, "history": 0.10}


class Match(BaseModel):
    need_id: str
    vendor_id: str
    vendor: str
    buyer: str
    eligible: bool
    score: float = 0.0
    breakdown: Dict[str, float] = {}
    reasons: List[str] = []       # why recommended
    gaps: List[str] = []          # what's missing / why excluded

    def explain(self) -> str:
        head = (f"**{self.vendor} → {self.buyer}** score {self.score:.2f}" if self.eligible
                else f"**{self.vendor}** not eligible")
        lines = [head] + [f"  ✓ {r}" for r in self.reasons] + [f"  ✗ {g}" for g in self.gaps]
        return "\n".join(lines)


# ---------- semantic similarity ----------
_TOKEN = re.compile(r"[a-z0-9]+")


def _bow(text: str) -> Counter:
    return Counter(t for t in _TOKEN.findall(text.lower()) if len(t) > 2)


def lexical_similarity(a: str, b: str) -> float:
    x, y = _bow(a), _bow(b)
    dot = sum(x[t] * y[t] for t in x)
    n = math.sqrt(sum(v * v for v in x.values())) * math.sqrt(sum(v * v for v in y.values()))
    return dot / n if n else 0.0


@lru_cache(maxsize=256)
def _gemini_embed(text: str):
    from google import genai
    r = genai.Client().models.embed_content(model=os.environ.get("EMBED_MODEL", "gemini-embedding-001"),
                                            contents=text)
    return tuple(r.embeddings[0].values)


def gemini_similarity(a: str, b: str) -> float:
    try:
        x, y = _gemini_embed(a), _gemini_embed(b)
    except Exception:
        return lexical_similarity(a, b)
    dot = sum(i * j for i, j in zip(x, y))
    n = math.sqrt(sum(i * i for i in x)) * math.sqrt(sum(j * j for j in y))
    return max(0.0, dot / n) if n else 0.0


def default_similarity() -> Callable[[str, str], float]:
    return gemini_similarity if os.environ.get("MATCH_SEMANTIC") == "gemini" else lexical_similarity


# ---------- scoring ----------
def _cover(wanted: List[str], have: List[str]) -> List[str]:
    h = {x.lower() for x in have}
    return [w for w in wanted if w.lower() not in h]


def evaluate(need: BuyerNeed, v: ProductListing,
             similarity: Optional[Callable[[str, str], float]] = None) -> Match:
    sim = similarity or default_similarity()
    m = Match(need_id=need.need_id, vendor_id=v.vendor_id, vendor=v.name, buyer=need.buyer, eligible=True)

    # 1) Hard filters (vendor eligibility + must-haves)
    if v.category != need.category:
        m.gaps.append(f"category {v.category} ≠ {need.category}")
    if need.region not in v.regions:
        m.gaps.append(f"does not serve region {need.region}")
    for kind in ("capabilities", "integrations", "certifications"):
        missing = _cover(getattr(need.must_have, kind), getattr(v, kind))
        if missing:
            m.gaps.append(f"missing required {kind}: {', '.join(missing)}")
    if need.must_have.min_sla_uptime and v.sla_uptime < need.must_have.min_sla_uptime:
        m.gaps.append(f"SLA {v.sla_uptime}% below required {need.must_have.min_sla_uptime}%")
    if m.gaps:
        m.eligible = False
        return m

    # 2) Soft scoring
    req = need.must_have.capabilities + need.nice_to_have.capabilities
    caps_hit = [c for c in req if c not in _cover(req, v.capabilities)]
    ints = need.must_have.integrations + need.nice_to_have.integrations
    ints_hit = [i for i in ints if i not in _cover(ints, v.integrations)]
    b = {
        "capabilities": len(caps_hit) / len(req) if req else 1.0,
        "integrations": len(ints_hit) / len(ints) if ints else 1.0,
        "semantic": sim(need.summary, f"{v.product}. {v.description} {' '.join(v.capabilities)}"),
        "industry": 1.0 if need.industry in v.industries else 0.0,
        "size": 1.0 if need.company_size in v.company_sizes else 0.0,
        "geography": 1.0,
        "intent": INTENT[need.buying_intent],
        "history": min(1.0, v.history.win_rate / 0.5) * min(1.0, v.history.deals_closed / 20),
    }
    m.breakdown = {k: round(x, 2) for k, x in b.items()}
    m.score = round(sum(WEIGHTS[k] * x for k, x in b.items()), 3)

    m.reasons.append(f"meets all must-haves ({', '.join(need.must_have.capabilities + need.must_have.integrations)})")
    extra = [c for c in caps_hit if c in need.nice_to_have.capabilities] + \
            [i for i in ints_hit if i in need.nice_to_have.integrations]
    if extra:
        m.reasons.append(f"covers nice-to-haves: {', '.join(extra)}")
    if b["industry"]:
        m.reasons.append(f"serves the {need.industry} industry")
    if b["size"]:
        m.reasons.append(f"sells to {need.company_size} companies")
    m.reasons.append(f"semantic fit {b['semantic']:.2f} with the buyer's description")
    m.reasons.append(f"track record: {v.history.deals_closed} deals, {v.history.win_rate:.0%} win rate")
    missing_nice = _cover(need.nice_to_have.capabilities, v.capabilities)
    if missing_nice:
        m.gaps.append(f"lacks nice-to-have: {', '.join(missing_nice)}")
    if not b["industry"]:
        m.gaps.append(f"no {need.industry} customers listed")
    return m


def rank(registry: Registry, need_id: str, include_ineligible: bool = False, **kw) -> List[Match]:
    need = registry.needs[need_id]
    ms = [evaluate(need, v, **kw) for v in registry.vendors.values()]
    ms.sort(key=lambda m: (m.eligible, m.score), reverse=True)
    return ms if include_ineligible else [m for m in ms if m.eligible]


def opportunities_for(registry: Registry, vendor_id: str, **kw) -> List[Match]:
    """Seller view: open needs this vendor is eligible for, best first."""
    v = registry.vendors[vendor_id]
    ms = [evaluate(n, v, **kw) for n in registry.open_needs()]
    return sorted([m for m in ms if m.eligible], key=lambda m: m.score, reverse=True)
