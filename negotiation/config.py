from __future__ import annotations

from pathlib import Path
from typing import Dict

import yaml

from .models import PrivatePolicy, PublicProfile

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "configs"


def load_public(company_id: str) -> PublicProfile:
    return PublicProfile(**yaml.safe_load((CONFIG / "public" / f"{company_id}.yaml").read_text()))


def load_private(company_id: str) -> PrivatePolicy:
    """Load from configs/private (gitignored), falling back to the example files."""
    for folder in ("private", "private.example"):
        p = CONFIG / folder / f"{company_id}.yaml"
        if p.exists():
            return PrivatePolicy(**yaml.safe_load(p.read_text()))
    raise FileNotFoundError(company_id)


def load_all(ids=("company_a", "company_b", "vendor_retailiq")) -> Dict[str, PrivatePolicy]:
    return {i: load_private(i) for i in ids if (CONFIG / "public" / f"{i}.yaml").exists()}


def has_agent(company_id: str) -> bool:
    """A company can negotiate if it has a public profile and private limits."""
    return (CONFIG / "public" / f"{company_id}.yaml").exists() and any(
        (CONFIG / f / f"{company_id}.yaml").exists() for f in ("private", "private.example"))
