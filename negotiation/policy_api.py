"""HTTP wrapper for the Policy Service. Run: uvicorn negotiation.policy_api:app --port 8100
Only the coordinator should hold POLICY_API_KEY. Keep this off the public internet."""
from __future__ import annotations

import os
from typing import Dict

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import load_all
from .policy_service import CheckResult, PolicyService

app = FastAPI(title="Policy Service")
_svc = PolicyService(load_all())


class Req(BaseModel):
    company_id: str
    terms: Dict


def _auth(key: str):
    if not key or key != os.environ.get("POLICY_API_KEY"):
        raise HTTPException(401, "unauthorized")


@app.post("/check", response_model=CheckResult)
def check(req: Req, x_api_key: str = Header("")):
    _auth(x_api_key)
    return _svc.check(req.company_id, req.terms)


@app.post("/approvers")
def approvers(req: Req, x_api_key: str = Header("")):
    _auth(x_api_key)
    return {"roles": _svc.approvers(req.company_id, req.terms)}
