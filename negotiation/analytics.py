"""Negotiation analytics: per-deal metrics computed from the coordinator's audit
trail, plus portfolio-level aggregates across all recorded deals.

Everything here is derived from public protocol events and the agents' non-secret
guardrail counters, so it is safe to post in the room.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .models import total_value

POLICY_CODES = {"OUT_OF_RANGE", "NON_NEGOTIABLE_VIOLATED", "CONSTRAINT_VIOLATED"}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _pct(part: float, whole: float) -> Optional[float]:
    return round(100.0 * part / whole, 2) if whole else None


def compute(audit: List[dict], seller: str, buyer: str,
            agent_meta: Dict[str, Optional[dict]], final_terms: Optional[dict]) -> dict:
    """Metrics for one negotiation. `audit` is the coordinator's event list."""
    proposals = [e for e in audit if e["event"] == "proposal" and e.get("terms")]
    statuses = [e for e in audit if e["event"] == "status"]
    first = {cid: next((e["terms"] for e in proposals if e["sender"] == cid), None) for cid in (seller, buyer)}

    opened = next((e["at"] for e in statuses if e["status"] == "NEGOTIATING"), audit[0]["at"] if audit else None)
    agreed = next((e["at"] for e in statuses if e["status"] == "TENTATIVE_AGREEMENT"), None)
    ended = next((e for e in reversed(statuses)
                  if e["status"] in ("FINALIZED", "REJECTED", "IMPASSE", "PENDING_APPROVAL")), None)
    final_status = ended["status"] if ended else "NEGOTIATING"

    rejections = [e for e in audit if e["event"] == "rejected_message"]
    coord_policy = sum(1 for e in rejections if POLICY_CODES & set(e.get("codes", [])))
    meta = {cid: (agent_meta.get(cid) or {}) for cid in (seller, buyer)}
    agent_policy = sum(m.get("policy_blocks", 0) for m in meta.values())
    leaks = sum(m.get("leak_blocks", 0) for m in meta.values())
    walked = any(e["event"] == "walk_away" for e in audit)

    s0, b0 = first[seller], first[buyer]
    out = {
        "outcome": final_status,
        "agreement_reached": agreed is not None,
        "rounds": max([e.get("round", 0) for e in proposals] or [0]),
        "seller_initial_price": s0 and s0["unit_price"],
        "buyer_initial_price": b0 and b0["unit_price"],
        "final_price": final_terms and final_terms["unit_price"],
        "seller_initial_total": s0 and total_value(s0),
        "buyer_initial_total": b0 and total_value(b0),
        "final_total": final_terms and total_value(final_terms),
        "time_to_agreement_s": round((_ts(agreed) - _ts(opened)).total_seconds(), 1) if agreed and opened else None,
        # offers the other side countered instead of accepting
        "rejected_offers": len(proposals) - (1 if agreed else 0),
        "coordinator_rejections": len(rejections),
        "policy_violations": coord_policy + agent_policy,
        "policy_violations_breakdown": {"blocked_by_coordinator": coord_policy, "blocked_by_agent_guardrails": agent_policy},
        "secret_leak_attempts": leaks,
        "walked_away": walked,
    }
    if s0 and final_terms:
        out["seller_discount_pct"] = _pct(s0["unit_price"] - final_terms["unit_price"], s0["unit_price"])
        # Savings on the deal actually signed: what the buyer would have paid at the
        # seller's opening price for the final scope, minus what it will pay.
        at_opening_price = dict(final_terms, unit_price=s0["unit_price"])
        out["buyer_savings"] = round(total_value(at_opening_price) - total_value(final_terms), 2)
        out["buyer_savings_per_seat"] = round(s0["unit_price"] - final_terms["unit_price"], 2)
    if b0 and final_terms:
        # how far the final price moved from the buyer's opening position (buyer's concession)
        out["buyer_concession_pct"] = _pct(final_terms["unit_price"] - b0["unit_price"], b0["unit_price"])
    if s0 and b0 and final_terms and s0["unit_price"] != b0["unit_price"]:
        # where the deal landed in the opening gap: 0% = buyer's open, 100% = seller's open
        out["price_split_pct"] = _pct(final_terms["unit_price"] - b0["unit_price"], s0["unit_price"] - b0["unit_price"])
    return out


def save(data_dir: Path, negotiation_id: str, metrics: dict) -> Path:
    d = data_dir / "analytics"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{negotiation_id}.json"
    p.write_text(json.dumps({"negotiation_id": negotiation_id, **metrics}, indent=2))
    return p


def summarize(data_dir: Path) -> dict:
    """Aggregate metrics across every recorded negotiation."""
    rows = [json.loads(p.read_text()) for p in sorted((data_dir / "analytics").glob("*.json"))] \
        if (data_dir / "analytics").exists() else []
    n = len(rows)

    def avg(key, subset=None):
        vals = [r[key] for r in (subset or rows) if r.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    deals = [r for r in rows if r.get("agreement_reached")]
    return {
        "negotiations": n,
        "agreements": len(deals),
        "agreement_rate_pct": _pct(len(deals), n),
        "finalized": sum(r["outcome"] == "FINALIZED" for r in rows),
        "rejected_by_humans": sum(r["outcome"] == "REJECTED" for r in rows),
        "impasses": sum(r["outcome"] == "IMPASSE" for r in rows),
        "walk_away_rate_pct": _pct(sum(bool(r.get("walked_away")) for r in rows), n),
        "avg_rounds": avg("rounds"),
        "avg_time_to_agreement_s": avg("time_to_agreement_s", deals),
        "avg_seller_discount_pct": avg("seller_discount_pct", deals),
        "avg_buyer_savings": avg("buyer_savings", deals),
        "total_contract_value": round(sum(r.get("final_total") or 0 for r in deals), 2),
        "total_policy_violations": sum(r.get("policy_violations", 0) for r in rows),
        "total_secret_leak_attempts": sum(r.get("secret_leak_attempts", 0) for r in rows),
    }


def report(m: dict) -> str:
    """Human-readable one-deal report."""
    def money(v):
        return "–" if v is None else f"${v:,.2f}"

    lines = [
        f"Outcome: {m['outcome']}  ·  rounds: {m['rounds']}  ·  time to agreement: "
        f"{'–' if m['time_to_agreement_s'] is None else str(m['time_to_agreement_s']) + 's'}",
        f"Price/seat: seller opened {money(m['seller_initial_price'])}, buyer opened {money(m['buyer_initial_price'])}, "
        f"final {money(m['final_price'])}",
    ]
    if m.get("buyer_savings") is not None:
        lines.append(f"Buyer savings vs seller's opening price: {money(m['buyer_savings'])} "
                     f"({money(m['buyer_savings_per_seat'])}/seat)  ·  seller discount: {m['seller_discount_pct']}%")
    if m.get("price_split_pct") is not None:
        lines.append(f"Deal landed {m['price_split_pct']}% of the way from the buyer's opening price to the seller's")
    lines.append(f"Rejected offers: {m['rejected_offers']}  ·  coordinator rejections: {m['coordinator_rejections']}  ·  "
                 f"policy violations blocked: {m['policy_violations']}  ·  secret-leak attempts blocked: "
                 f"{m['secret_leak_attempts']}  ·  walked away: {'yes' if m['walked_away'] else 'no'}")
    return "\n".join(lines)


def summary_report(s: dict) -> str:
    if not s["negotiations"]:
        return "No negotiations recorded yet."
    def f(v, unit=""):
        return "–" if v is None else f"{v}{unit}"
    savings = "–" if s["avg_buyer_savings"] is None else f"${s['avg_buyer_savings']:,.0f}"
    return "\n".join([
        f"Negotiations: {s['negotiations']}  ·  agreements: {s['agreements']} ({f(s['agreement_rate_pct'], '%')})  ·  "
        f"finalized: {s['finalized']}  ·  rejected by humans: {s['rejected_by_humans']}  ·  impasses: {s['impasses']}",
        f"Walk-away rate: {f(s['walk_away_rate_pct'], '%')}  ·  avg rounds: {f(s['avg_rounds'])}  ·  "
        f"avg time to agreement: {f(s['avg_time_to_agreement_s'], 's')}",
        f"Avg seller discount: {f(s['avg_seller_discount_pct'], '%')}  ·  avg buyer savings: {savings}  ·  "
        f"total contract value: ${s['total_contract_value']:,.0f}",
        f"Policy violations blocked: {s['total_policy_violations']}  ·  secret-leak attempts blocked: "
        f"{s['total_secret_leak_attempts']}",
    ])


if __name__ == "__main__":  # python -m negotiation.analytics
    from .coordinator import DATA
    print(summary_report(summarize(DATA)))
