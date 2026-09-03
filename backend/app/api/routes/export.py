"""Report export API (spec #19) — PDF / JSON / CSV.

Endpoints (all attach as downloads, all audit-logged):
    GET /api/verify/{job_id}/export.pdf
    GET /api/verify/{job_id}/export.json
    GET /api/verify/{job_id}/export.csv

The PDF renders with Noto Sans SC so user-supplied CJK/extended content
survives; every string is XML-escaped before it touches a Paragraph, and
control/emoji characters are stripped (they cannot render in embedded
fonts). Exported reports always include the Limitations & provenance
section — honesty travels with the artifact.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.database import get_db
from app.db.models import User
from app.db.repositories import AuditRepository
from app.core.security import get_current_user

logger = get_logger("prvision.api.export")

router = APIRouter(tags=["export"])

_CONTROL_OR_EMOJI = re.compile(
    "[\ud800-\udfff\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\u200b-\u200f\u2028\u2029\uFEFF]")

_LIMITATIONS = [
    "PR•VISION provides probabilistic decision support for human investigators — "
    "verdicts are evidence-weighted estimates, not ground truth.",
    "Evidence comes only from the providers configured at analysis time. Providers "
    "shown as DISABLED were skipped; results were never fabricated to fill gaps.",
    "Media signals (OCR quality, EXIF, manipulation and AI-generation heuristics) "
    "are indicators, not proof; a clean result does not certify authenticity.",
    "Spread forecasts (XGBoost, 30/60/120 min) are statistical estimates with "
    "quantified confidence and can deviate materially from real outcomes.",
    "Source quality scoring is a modelled heuristic over observable signals and "
    "must not be read as a domain allowlist/denylist.",
    "The absence of contradicting evidence must NOT be interpreted as confirmation.",
]


def _clean(text: Any, limit: int = 1200) -> str:
    """Make arbitrary content safe for Paragraphs/CSV (no control/emoji chars)."""
    if text is None:
        return ""
    s = str(text)
    s = _CONTROL_OR_EMOJI.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if ch == "\n" or ord(ch) >= 32)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return s[:limit]


def _fmt_date(value: Any) -> str:
    return str(value)[:19].replace("T", " ") if value else "—"


# ----------------------------------------------------------------------------- payload
def _load_report(job_id: int) -> dict[str, Any]:
    from app.api.routes.verify import job_report
    report = job_report(job_id)
    if not report.get("report_ready"):
        raise HTTPException(status_code=409, detail=f"Job is {report.get('status')} — report not ready")
    return report


def _limitations_for(report: dict[str, Any]) -> list[str]:
    out = list(_LIMITATIONS)
    fetch = ((report.get("content") or {}).get("fetch_status")) or ""
    if fetch and fetch != "ok":
        out.append(f"Content fetch status was “{fetch}” — some analysis may be based on "
                   "partial or unavailable primary content.")
    providers = report.get("providers") or []
    names: list[str] = []
    if isinstance(providers, dict):
        # shape: {provider_name: "CONNECTED"|"DISABLED"|"UNAVAILABLE"|...}
        for name, state in providers.items():
            if name == "note" or not isinstance(state, str):
                continue
            if state.upper() in ("DISABLED", "UNAVAILABLE"):
                names.append(str(name))
    elif isinstance(providers, list):
        for p in providers:
            if isinstance(p, dict) and p.get("state") in ("DISABLED", "UNAVAILABLE"):
                names.append(str(p.get("name")))
            elif isinstance(p, str):
                names.append(p)
    if names:
        out.append("Providers unavailable/disabled during this analysis: "
                   + ", ".join(names) + ".")
    return out


@router.get("/verify/{job_id}/export.json", summary="Export full report as JSON")
def export_json(job_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    report = _load_report(job_id)
    report["export"] = {
        "format": "json", "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_version": settings.APP_VERSION, "limitations": _limitations_for(report),
    }
    AuditRepository.record(db, actor=_actor(db, request), action="report.export",
                           target_type="verification_job", target_id=job_id, detail="json")
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    return _download(payload.encode("utf-8"), f"prvision-report-{job_id}.json",
                     "application/json")


@router.get("/verify/{job_id}/export.csv", summary="Export claims & evidence as CSV")
def export_csv(job_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    report = _load_report(job_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["record_type", "claim_ordinal", "claim_text", "claim_type",
                     "verdict", "confidence", "supporting", "contradicting",
                     "provider", "title", "publisher", "published_at", "stance",
                     "relevance", "url", "detail"])
    overall = report.get("overall") or {}
    for claim in report.get("claims") or []:
        verdict = claim.get("verdict") or {}
        writer.writerow([
            "claim", claim.get("ordinal"), _csv(claim.get("text")),
            _csv(claim.get("claim_type")), _csv(verdict.get("verdict")),
            verdict.get("confidence"), verdict.get("supporting_count"),
            verdict.get("contradicting_count"), "", "", "", "", "", "", "",
            _csv(verdict.get("explanation"), 500)])
        for ev in claim.get("evidence") or []:
            writer.writerow([
                "evidence", claim.get("ordinal"), "", "", _csv(verdict.get("verdict")),
                "", "", "", _csv(ev.get("provider")), _csv(ev.get("title")),
                _csv(ev.get("publisher")), _csv(ev.get("published_at")),
                _csv(ev.get("stance")), ev.get("relevance"), _csv(ev.get("url")),
                _csv(ev.get("snippet"), 400)])
        for fc in claim.get("fact_checks") or []:
            writer.writerow([
                "fact_check", claim.get("ordinal"), _csv(fc.get("claim_text")), "",
                "", "", "", "", _csv(fc.get("provider")), _csv(fc.get("textual_rating")),
                _csv(fc.get("publisher")), _csv(fc.get("published_at")), "", "",
                _csv(fc.get("url")), _csv(fc.get("review_snippet"), 400)])
    for check in report.get("numerical_checks") or []:
        writer.writerow(["numerical_check", "", _csv(check.get("subject")), "", "",
                        "", "", "", "", "", "", "", "", "",
                        _csv(check.get("observed")),
                        f"{check.get('check_type')}: {check.get('status')} — {check.get('detail')}"])
    AuditRepository.record(db, actor=_actor(db, request), action="report.export",
                           target_type="verification_job", target_id=job_id, detail="csv")
    return _download(buf.getvalue().encode("utf-8-sig"), f"prvision-report-{job_id}.csv", "text/csv")


def _csv(value: Any, limit: int = 800) -> str:
    """CSV cells: keep raw text (no XML escaping), strip newlines/control."""
    s = _CONTROL_OR_EMOJI.sub("", unicodedata.normalize("NFKC", str(value or "")))
    return s.replace("\r", " ").replace("\n", " ").strip()[:limit]


# ----------------------------------------------------------------------------- pdf
def _download(data: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=data, media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"",
                 "Cache-Control": "no-store"})


def _actor(db: Session, request: Request) -> str:
    user = get_current_user(request, db)
    return user.email if user else "anonymous"


@router.get("/verify/{job_id}/export.pdf", summary="Export investigator report as PDF")
def export_pdf(job_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    report = _load_report(job_id)
    pdf_bytes = build_pdf(report)
    AuditRepository.record(db, actor=_actor(db, request), action="report.export",
                           target_type="verification_job", target_id=job_id, detail="pdf")
    return _download(pdf_bytes, f"prvision-report-{job_id}.pdf", "application/pdf")


def _register_fonts() -> Any:
    """Register Noto Sans SC (CJK-safe). Falls back to Helvetica silently."""
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        "/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/chinese/NotoSansSC-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ]
    regular = next((p for p in candidates if _exists(p)), None)
    if regular is None:
        return None
    try:
        pdfmetrics.registerFont(TTFont("NotoSans", regular))
        bold = next((p for p in bold_candidates if _exists(p)), regular)
        pdfmetrics.registerFont(TTFont("NotoSans-Bold", bold))
        addMapping("NotoSans", 0, 0, "NotoSans")
        addMapping("NotoSans", 1, 0, "NotoSans-Bold")
        return "NotoSans"
    except Exception:
        logger.warning("Could not register Noto fonts; falling back to Helvetica")
        return None


def _exists(path: str) -> bool:
    import os
    return os.path.exists(path)


def build_pdf(report: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (HRFlowable, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    font = _register_fonts() or "Helvetica"
    base = ParagraphStyle("base", fontName=font, fontSize=9.5, leading=13.5, alignment=TA_LEFT)
    h1 = ParagraphStyle("h1", parent=base, fontName=(font + "-Bold") if font != "Helvetica" else "Helvetica-Bold",
                        fontSize=19, leading=24, textColor=colors.HexColor("#0B1F33"))
    h2 = ParagraphStyle("h2", parent=h1, fontSize=12.5, leading=16,
                        textColor=colors.HexColor("#0E7490"), spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=base, spaceAfter=3)
    small = ParagraphStyle("small", parent=base, fontSize=8, leading=11,
                           textColor=colors.HexColor("#475569"))
    cell = ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11.5)
    cell_head = ParagraphStyle("cellh", parent=cell, fontName=(font + "-Bold") if font != "Helvetica" else "Helvetica-Bold")

    accent = colors.HexColor("#0E7490")
    line = colors.HexColor("#CBD5E1")
    soft = colors.HexColor("#F1F5F9")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"PR•VISION Verification Report #{report.get('job_id')}",
        author="PR•VISION")
    story: list = []

    content = report.get("content") or {}
    overall = report.get("overall") or {}
    priority = report.get("priority") or {}
    risk = report.get("risk") or {}

    story.append(Paragraph("PR•VISION — VERIFICATION REPORT", h1))
    story.append(Paragraph(
        f"Job #{report.get('job_id')} · {_fmt_date(report.get('generated_at')) or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} · "
        f"input: {report.get('input_kind', '—')} · engine v{settings.APP_VERSION}", small))
    story.append(HRFlowable(width="100%", color=accent, thickness=1.2, spaceAfter=8))

    # ---- input
    story.append(Paragraph("1. ANALYSED INPUT", h2))
    story.append(Paragraph(_clean(content.get("title") or report.get("input_label") or "Untitled"), body))
    meta_rows = [
        ["Publisher / author", _clean(" / ".join(x for x in [content.get("publisher"), content.get("author")] if x) or "—")],
        ["Published", _fmt_date(content.get("published_at"))],
        ["Canonical URL", _clean(content.get("canonical_url") or content.get("original_url") or "—")],
        ["Fetch status", _clean(content.get("fetch_status") or "—") + (f" (HTTP {content.get('http_status')})" if content.get("http_status") else "")],
        ["Source classification", _clean(content.get("source_classification") or "—")],
    ]
    story.append(_table(meta_rows, cell, cell_head, soft, line))

    # ---- verdict
    story.append(Paragraph("2. OVERALL VERDICT", h2))
    verdict = _clean(overall.get("verdict") or "UNVERIFIED")
    story.append(Paragraph(
        f"<font size=13><b>{verdict}</b></font>", body))
    if overall.get("detail"):
        story.append(Paragraph(_clean(overall.get("detail")), body))
    caveats = overall.get("caveats") or []
    for c in caveats[:6]:
        story.append(Paragraph("• " + _clean(c), small))

    # ---- priority
    if priority:
        story.append(Paragraph("3. INTERVENTION PRIORITY", h2))
        score = priority.get("intervention_priority", priority.get("score"))
        label = priority.get("label") or ""
        story.append(Paragraph(
            f"<b>{score if score is not None else '—'}/100</b> — {label}", body))
        for factor in (priority.get("factors") or priority.get("reasons") or [])[:6]:
            story.append(Paragraph("• " + _clean(factor if isinstance(factor, str) else json.dumps(factor)), small))
    if risk:
        story.append(Paragraph(
            f"Misinformation risk (engine): <b>{risk.get('misinformation_risk', '—')}</b> · "
            f"confidence: {risk.get('confidence', '—')}", small))

    # ---- claims
    claims = report.get("claims") or []
    if claims:
        story.append(Paragraph("4. CLAIM-LEVEL VERDICTS & EVIDENCE", h2))
        for claim in claims:
            v = claim.get("verdict") or {}
            head = (f"#{claim.get('ordinal')} [{_clean(v.get('verdict') or 'UNVERIFIED')}"
                    f" · conf {v.get('confidence', '—')}]")
            story.append(Paragraph(f"<b>{head}</b>", cell))
            story.append(Paragraph(_clean(claim.get("text"), 500), cell))
            for ev in (claim.get("evidence") or [])[:5]:
                story.append(Paragraph(
                    f"↳ <i>{_clean(ev.get('stance') or 'related')}</i> — "
                    f"{_clean(ev.get('title'), 120)} ({_clean(ev.get('publisher') or ev.get('provider'), 60)}"
                    f"{', ' + _fmt_date(ev.get('published_at'))[:10] if ev.get('published_at') else ''}) "
                    f"<font color='#0E7490'>{_clean(ev.get('url'), 110)}</font>", small))
            for fc in (claim.get("fact_checks") or [])[:3]:
                story.append(Paragraph(
                    f"↳ fact-check: <i>{_clean(fc.get('textual_rating'), 80)}</i> — "
                    f"{_clean(fc.get('publisher'), 60)} <font color='#0E7490'>{_clean(fc.get('url'), 110)}</font>", small))
            story.append(Spacer(1, 4))

    # ---- media
    media = report.get("media") or []
    if media:
        story.append(Paragraph("5. MEDIA FORENSICS", h2))
        for m in media:
            analysis = m.get("analysis") or {}
            story.append(Paragraph(
                f"<b>{_clean(m.get('filename') or m.get('media_type'), 80)}</b> · "
                f"manipulation risk {m.get('manipulation_risk', '—')} · "
                f"AI signal {m.get('ai_generation_signal') or '—'}"
                f" (conf {m.get('ai_signal_confidence', '—')})", cell))
            if analysis.get("exif"):
                exif_items = list(analysis["exif"].items())[:6]
                story.append(Paragraph("EXIF: " + _clean("; ".join(f"{k}={v}" for k, v in exif_items), 300), small))
            if m.get("ocr_text"):
                story.append(Paragraph("OCR: " + _clean(m["ocr_text"], 400), small))
            story.append(Paragraph(_clean(m.get("authenticity_note"), 300), small))
            story.append(Spacer(1, 3))

    # ---- numerical checks
    numerics = report.get("numerical_checks") or []
    if numerics:
        story.append(Paragraph("6. NUMERICAL CHECKS (DETERMINISTIC)", h2))
        rows = [["Subject", "Expected", "Observed", "Status", "Detail"]] + [
            [_clean(n.get("subject"), 60), _clean(n.get("expected"), 40),
             _clean(n.get("observed"), 40), _clean(n.get("status"), 20),
             _clean(n.get("detail"), 140)] for n in numerics[:20]]
        story.append(_table(rows, cell, cell_head, soft, line, font_sizes=(8, 8.5)))

    # ---- timeline
    timeline = report.get("timeline") or []
    if timeline:
        story.append(Paragraph("7. EVIDENCE TIMELINE", h2))
        rows = [["When", "Event", "Detail"]] + [
            [_fmt_date(t.get("occurred_at")), _clean(t.get("label"), 60),
             _clean(t.get("detail"), 160)] for t in timeline[:25]]
        story.append(_table(rows, cell, cell_head, soft, line, font_sizes=(8, 8.5)))

    # ---- providers
    providers = report.get("providers") or []
    if providers:
        story.append(Paragraph("8. EVIDENCE PROVIDERS & PROVENANCE", h2))
        rows = [["Provider", "State", "Detail"]]
        if isinstance(providers, dict):
            for name, state in providers.items():
                rows.append([_clean(name, 40), _clean(state, 20), ""])
        else:
            rows.extend([[_clean(p.get("name"), 40), _clean(p.get("state"), 20),
                          _clean(p.get("detail"), 120)] for p in providers if isinstance(p, dict)])
        story.append(_table(rows, cell, cell_head, soft, line, font_sizes=(8, 8.5)))

    # ---- limitations
    story.append(Paragraph("9. LIMITATIONS & HONESTY NOTES", h2))
    for lim in _limitations_for(report):
        story.append(Paragraph("• " + _clean(lim, 400), small))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", color=line, thickness=0.7))
    story.append(Paragraph(
        "Generated by PR•VISION — probabilistic decision support. "
        "Every conclusion carries evidence, confidence and reasoning; none of it "
        "should be treated as an automated truth verdict.", small))

    def _footer(canvas, doc_):  # noqa: ANN001
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(18 * mm, 10 * mm, f"PR•VISION report #{report.get('job_id')} — "
                                            f"generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _table(rows: list[list[str]], cell, cell_head, soft, line, font_sizes=None) -> Any:
    from reportlab.platypus import Paragraph, Table, TableStyle
    from reportlab.lib import colors
    head_size = font_sizes[1] if font_sizes else 8.5
    body_size = font_sizes[0] if font_sizes else 8.5
    data = []
    for i, row in enumerate(rows):
        style = cell_head if i == 0 else cell
        data.append([Paragraph(str(c), style) for c in row])
    t = Table(data, colWidths=None, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), soft),
        ("GRID", (0, 0), (-1, -1), 0.4, line),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), body_size),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t
