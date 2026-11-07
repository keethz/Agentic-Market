"""Agent & product registry: public vendor listings and buyer needs.

Everything here is PUBLIC. Buyers publish requirements and a wide budget band,
never their true maximum budget (that stays in configs/private/)."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .config import CONFIG


class History(BaseModel):
    deals_closed: int = 0
    win_rate: float = 0.0
    avg_rounds: float = 0.0


class ProductListing(BaseModel):
    vendor_id: str
    name: str
    product: str
    category: str
    capabilities: List[str] = []
    integrations: List[str] = []
    industries: List[str] = []
    company_sizes: List[str] = []
    regions: List[str] = []
    certifications: List[str] = []
    sla_uptime: float = 0.0
    deployment: List[str] = []
    description: str = ""
    history: History = History()


class Requirements(BaseModel):
    capabilities: List[str] = []
    integrations: List[str] = []
    certifications: List[str] = []
    min_sla_uptime: Optional[float] = None


class BuyerNeed(BaseModel):
    need_id: str
    buyer_id: str
    buyer: str
    category: str
    summary: str
    industry: str
    company_size: str
    region: str
    seats: Dict[str, int] = {}
    must_have: Requirements = Requirements()
    nice_to_have: Requirements = Requirements()
    budget_band: str = ""
    buying_intent: Literal["low", "medium", "high"] = "medium"
    timeline: str = ""
    status: Literal["open", "closed"] = "open"


class Registry:
    def __init__(self, vendors: List[ProductListing], needs: List[BuyerNeed]):
        self.vendors = {v.vendor_id: v for v in vendors}
        self.needs = {n.need_id: n for n in needs}

    @classmethod
    def load(cls, folder: Path = CONFIG / "registry") -> "Registry":
        read = lambda f: yaml.safe_load((folder / f).read_text()) or []
        return cls([ProductListing(**v) for v in read("vendors.yaml")],
                   [BuyerNeed(**n) for n in read("needs.yaml")])

    def publish_need(self, need: BuyerNeed):
        self.needs[need.need_id] = need

    def publish_listing(self, listing: ProductListing):
        self.vendors[listing.vendor_id] = listing

    def open_needs(self) -> List[BuyerNeed]:
        return [n for n in self.needs.values() if n.status == "open"]

    def needs_for(self, buyer_id: str) -> List[BuyerNeed]:
        return [n for n in self.open_needs() if n.buyer_id == buyer_id]
