"""Render protocol payloads as human-readable Band messages with an embedded JSON block,
and parse them back. Humans read the summary line; agents read the JSON."""
from __future__ import annotations

import json
import re
from typing import Optional

from .models import total_value

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def terms_line(t: dict) -> str:
    return (f"${t['unit_price']}/seat × {t['quantity']} seats · {t['contract_months']} mo · "
            f"net {t['payment_terms_days']} · {t['sla_uptime']}% SLA · {t['support_tier']} support "
            f"(total ${total_value(t):,.0f})")


def render(payload: dict) -> str:
    kind = payload.get("kind") or payload.get("type")
    t = payload.get("terms")
    if kind == "status":
        head = f"📌 Status: **{payload['status']}** — {payload.get('reason', '')}"
    elif kind == "approval_request":
        roles = ", ".join(f"{a['company']} / {a['role']}" for a in payload["required_approvers"])
        head = (f"✅ Agreement reached. Contract **{payload['contract_id']}** — {terms_line(t)}\n"
                f"Constraints satisfied: yes · Hash `{payload['terms_hash'][:12]}`\n"
                f"Required approvers: {roles}\n"
                f"Reply `@Deal Coordinator approve <role>` or `@Deal Coordinator reject <role> <reason>`.")
    elif kind == "error":
        head = f"⚠️ Rejected: {', '.join(payload['codes'])}"
    elif kind == "start":
        head = "🔔 Negotiation opened. You make the first offer."
    elif t:
        who = payload.get("from", "")
        head = f"{(kind or '').replace('_', ' ').title()}{' from ' + who if who else ''}: {terms_line(t)}"
        if payload.get("rationale"):
            head += f"\n> {payload['rationale']}"
    else:
        head = f"{kind}: {payload.get('rationale', '')}"
    return f"{head}\n```json\n{json.dumps(payload)}\n```"


def parse(content: str) -> Optional[dict]:
    m = _JSON_BLOCK.search(content)
    raw = m.group(1) if m else content[content.find("{"):] if "{" in content else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
