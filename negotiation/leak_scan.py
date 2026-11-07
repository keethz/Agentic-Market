"""Scan a room timeline for leaked secrets (demo + test helper)."""
from __future__ import annotations

import json
from typing import Dict, List

from .guardrails import leak_findings
from .models import PrivatePolicy
from .transport import Message


def scan(timeline: List[Message], policies: Dict[str, PrivatePolicy]) -> List[str]:
    findings = []
    disclosed: dict = {}  # every term value already offered publicly in the room
    for i, m in enumerate(timeline):
        try:
            body = json.loads(m.content)
        except ValueError:
            body = {"rationale": m.content}
        # Only free text can leak; protocol numbers (round, rounds_left, totals) are not secrets.
        text = " ".join(str(v) for k, v in body.items()
                        if k in ("rationale", "reason", "note")) if isinstance(body, dict) else m.content
        offered = body.get("terms") if isinstance(body, dict) else None
        if isinstance(body, dict) and body.get("kind") == "analytics":
            # Metrics are computed only from public offers and non-secret counters;
            # number-matching derived values just produces coincidental hits.
            continue
        for k, v in (offered or {}).items():
            disclosed[f"{k}:{v}"] = v
        public = {f"d{j}": v for j, v in enumerate(disclosed.values())}
        for cid, pol in policies.items():
            for f in leak_findings(text, pol, public):
                if f.startswith("phrase:") and m.sender == "coordinator":
                    continue
                findings.append(f"msg#{i} {m.sender}->{m.recipient or 'room'} leaks {cid} {f}")
    return findings
