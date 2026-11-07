"""Scan a room timeline for leaked secrets (demo + test helper)."""
from __future__ import annotations

import json
from typing import Dict, List

from .guardrails import leak_findings
from .models import PrivatePolicy
from .transport import Message


def scan(timeline: List[Message], policies: Dict[str, PrivatePolicy]) -> List[str]:
    findings = []
    for i, m in enumerate(timeline):
        try:
            body = json.loads(m.content)
        except ValueError:
            body = {"rationale": m.content}
        # Only free text can leak; protocol numbers (round, rounds_left, totals) are not secrets.
        text = " ".join(str(v) for k, v in body.items()
                        if k in ("rationale", "reason", "note")) if isinstance(body, dict) else m.content
        offered = body.get("terms") if isinstance(body, dict) else None
        for cid, pol in policies.items():
            for f in leak_findings(text, pol, offered):
                if f.startswith("phrase:") and m.sender == "coordinator":
                    continue
                findings.append(f"msg#{i} {m.sender}->{m.recipient or 'room'} leaks {cid} {f}")
    return findings
