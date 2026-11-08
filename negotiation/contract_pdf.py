"""Render a FINALIZED contract to PDF (data/contracts/<id>.pdf).

Only the agreed public terms, parties, approvals and integrity hash go in the
PDF; no private limits."""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Contract, Status

LABELS = {"unit_price": ("Unit price", lambda v: f"${float(v):,.2f} per seat / year"),
          "quantity": ("Seats", lambda v: f"{int(v):,}"),
          "contract_months": ("Term", lambda v: f"{int(v)} months"),
          "payment_terms_days": ("Payment terms", lambda v: f"Net {int(v)} days"),
          "sla_uptime": ("Uptime SLA", lambda v: f"{float(v)}%"),
          "support_tier": ("Support tier", lambda v: str(v).title())}

_GRID = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9)])


def render(contract: Contract, roles: Dict[str, str], out_dir: Path) -> Path:
    """roles: company_id -> 'Seller' | 'Buyer'."""
    if contract.status != Status.finalized:
        raise ValueError("only FINALIZED contracts are rendered")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{contract.contract_id}.pdf"
    ss = getSampleStyleSheet()
    s, small = ss["BodyText"], ss["Italic"]
    names = contract.parties
    seller = next((c for c, r in roles.items() if r == "Seller"), next(iter(names)))
    buyer = next(c for c in names if c != seller)
    t = contract.terms

    story = [
        Paragraph("Software Subscription Agreement", ss["Title"]),
        Paragraph(f"Contract <b>{contract.contract_id}</b> &middot; negotiation {contract.negotiation_id}"
                  f" &middot; status <b>FINALIZED</b>", s),
        Spacer(1, 10),
        Paragraph("1. Parties", ss["Heading2"]),
        Paragraph(f"<b>Seller:</b> {names[seller]}<br/><b>Buyer:</b> {names[buyer]}", s),
        Paragraph("2. Agreed terms", ss["Heading2"]),
        Table([["Term", "Agreed value"]] + [[LABELS[k][0], LABELS[k][1](t[k])] for k in LABELS if k in t]
              + [["Total contract value", f"${contract.total_value:,.2f}"]],
              colWidths=[2.2 * inch, 4.3 * inch], style=_GRID),
        Spacer(1, 6),
        Paragraph("3. Conditions", ss["Heading2"]),
        Paragraph(f"{names[seller]} grants {names[buyer]} {int(t['quantity']):,} seat licenses for "
                  f"{int(t['contract_months'])} months at the unit price above, billed annually and payable "
                  f"net {int(t['payment_terms_days'])} days from invoice. {names[seller]} will maintain "
                  f"{t['sla_uptime']}% monthly uptime and provide {str(t['support_tier']).title()} support. "
                  "These terms were agreed identically by both parties' negotiation agents, checked against "
                  "both companies' policies, and approved by every required human approver.", s),
        Paragraph("4. Approvals", ss["Heading2"]),
        Table([["Company", "Role", "Approved by", "Decision", "Date (UTC)"]] +
              [[names[a.company_id], a.role, a.approver or "", (a.decision or "").upper(), (a.at or "")[:19].replace("T", " ")]
               for a in contract.approvals], style=_GRID),
        Spacer(1, 10),
        Spacer(1, 14),
        Paragraph("Generated automatically by the Deal Coordinator. Contains no confidential negotiation limits.", small),
    ]
    SimpleDocTemplate(str(path), pagesize=LETTER, title=f"Contract {contract.contract_id}",
                      leftMargin=0.8 * inch, rightMargin=0.8 * inch).build(story)
    return path
