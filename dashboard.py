"""Lightweight read-only dashboard of negotiations and closed deals.

  .venv/bin/python dashboard.py        # http://localhost:8050
"""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from negotiation.coordinator import DATA
from negotiation.dashboard_data import deals

app = FastAPI(title="Agentic Market dashboard")
PAGE = Path(__file__).with_name("dashboard.html")


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.read_text()


@app.get("/api/deals")
def api_deals():
    return deals()


@app.get("/contracts/{contract_id}.pdf")
def contract_pdf(contract_id: str):
    p = DATA / "contracts" / f"{contract_id}.pdf"
    if not contract_id.startswith("ctr-") or "/" in contract_id or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DASHBOARD_PORT", 8050)), log_level="warning")
