import pytest
from pypdf import PdfReader

from negotiation.config import load_all
from negotiation.contract_pdf import render
from negotiation.models import Status
from negotiation.system import build


def _deal(tmp_path):
    t, coord, a, b = build("scripted", data_dir=tmp_path)
    coord.start()
    t.run_until_idle()
    return coord


def test_pdf_created_only_when_finalized(tmp_path):
    coord = _deal(tmp_path)
    first, *rest = coord.contract.approvals
    coord.approve(first.company_id, first.role, "Alice")
    assert coord.contract_pdf is None
    for a in rest:
        coord.approve(a.company_id, a.role, "Bob")
    assert coord.status == Status.finalized and coord.contract_pdf.exists()

    text = "".join(p.extract_text() for p in PdfReader(coord.contract_pdf).pages)
    t = coord.contract.terms
    assert coord.contract.contract_id in text and "FINALIZED" in text
    assert f"{int(t['quantity']):,}" in text and f"Net {t['payment_terms_days']}" in text
    assert coord.contract.terms_hash in text.replace("\n", "")
    for a in coord.contract.approvals:
        assert a.role in text


def test_no_pdf_on_rejection(tmp_path):
    coord = _deal(tmp_path)
    a = coord.contract.approvals[0]
    coord.reject(a.company_id, a.role, "Alice", "no")
    assert coord.contract_pdf is None
    with pytest.raises(ValueError):
        render(coord.contract, {}, tmp_path)


def test_pdf_has_no_private_limits(tmp_path):
    coord = _deal(tmp_path)
    for a in coord.contract.approvals:
        coord.approve(a.company_id, a.role, "Bob")
    text = "".join(p.extract_text() for p in PdfReader(coord.contract_pdf).pages).lower()
    for word in ("budget", "floor", "walk-away", "target", "minimum"):
        assert word not in text
