"""Build dashboard rows from data/: audit log + analytics + contracts. Public data only."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .config import CONFIG
from .coordinator import DATA

import yaml


def _names() -> Dict[str, str]:
    out = {}
    for p in (CONFIG / "public").glob("*.yaml"):
        d = yaml.safe_load(p.read_text())
        out[d["id"]] = d["name"]
    return out


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def deals(data_dir: Path = DATA) -> List[dict]:
    names, negs = _names(), {}
    audit = data_dir / "audit.jsonl"
    for line in (audit.read_text().splitlines() if audit.exists() else []):
        e = json.loads(line)
        n = negs.setdefault(e["negotiation_id"], {"events": []})
        n["events"].append(e)

    contracts = {}
    for p in (data_dir / "contracts").glob("*.json"):
        c = json.loads(p.read_text())
        contracts[c["negotiation_id"]] = c

    rows = []
    for nid, n in negs.items():
        ev = n["events"]
        senders = [e["sender"] for e in ev if e["event"] == "proposal"]
        statuses = [e for e in ev if e["event"] == "status"]
        if not statuses:
            continue
        seller = senders[0] if senders else None
        buyer = next((s for s in senders if s != seller), None)
        c = contracts.get(nid)
        if c:
            seller = seller or next(iter(c["parties"]))
            buyer = buyer or next(k for k in c["parties"] if k != seller)
        a_path = data_dir / "analytics" / f"{nid}.json"
        m = json.loads(a_path.read_text()) if a_path.exists() else {}
        start = _ts(statuses[0]["at"])
        final = next((e for e in statuses if e["status"] == "FINALIZED"), None)
        rows.append({
            "negotiation_id": nid,
            "started_at": statuses[0]["at"],
            "seller": names.get(seller, seller or "?"),
            "buyer": names.get(buyer, buyer or "?"),
            "status": statuses[-1]["status"],
            "reason": statuses[-1].get("reason", ""),
            "rounds": m.get("rounds", len(senders)),
            "time_to_agreement_s": m.get("time_to_agreement_s"),
            "time_to_close_s": round((_ts(final["at"]) - start).total_seconds(), 1) if final else None,
            "final_price": m.get("final_price"),
            "total_value": c["total_value"] if c else None,
            "seller_discount_pct": m.get("seller_discount_pct"),
            "leak_attempts_blocked": m.get("secret_leak_attempts", 0),
            "policy_violations_blocked": m.get("policy_violations", 0),
            "contract_id": c["contract_id"] if c else None,
            "has_pdf": bool(c and (data_dir / "contracts" / f"{c['contract_id']}.pdf").exists()),
        })
    rows.sort(key=lambda r: r["started_at"], reverse=True)
    return rows
