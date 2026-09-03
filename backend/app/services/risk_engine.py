"""Multimodal misinformation risk engine (spec #33).

Modular, explainable fusion:

    text_risk + claim_risk + source_risk + evidence_conflict
    + image_risk + document_risk + propagation_risk + anomaly_risk
    -> Misinformation Risk 0-100, Confidence 0-100, Level

Every component returns {risk, weight, detail} so the report can show the
contribution of each signal. Levels: LOW | MEDIUM | HIGH | CRITICAL.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger("prvision.risk")

_LEVELS = ((0.75, "CRITICAL"), (0.5, "HIGH"), (0.28, "MEDIUM"))

_SENSATIONAL = re.compile(
    r"\b(shocking|unbelievable|bombshell|explosive|you won'?t believe|"
    r"banned|censored|they don'?t want|miracle|cure|secret|exposed|"
    r"breaking|urgent|must share|before it'?s deleted)\b",
    re.IGNORECASE,
)
_MANIPULATIVE = re.compile(
    r"\b(share (?:this )?(?:before|now|immediately)|forward to everyone|copy (?:and|&) paste|"
    r"delete(?:d)? (?:soon|this)| mainstream media won'?t|wake up|sheeple)\b",
    re.IGNORECASE,
)


def _level(x: float) -> str:
    for threshold, label in _LEVELS:
        if x >= threshold:
            return label
    return "LOW"


def text_risk(text: str) -> dict[str, Any]:
    """Sensational / manipulative language density (lexical, transparent)."""
    if not text:
        return {"risk": 0.0, "weight": 0.8, "detail": "no text"}
    words = max(1, len(text.split()))
    sens = len(_SENSATIONAL.findall(text))
    manip = len(_MANIPULATIVE.findall(text))
    density = (sens * 1.0 + manip * 1.6) / max(10.0, words / 40.0)
    risk = min(1.0, density / 6.0)
    detail = f"{sens} sensational + {manip} manipulative markers in {words} words"
    return {"risk": round(risk, 3), "weight": 1.0, "detail": detail}


def claim_risk(claim_assessments: list[dict[str, Any]]) -> dict[str, Any]:
    """Share of checkable claims assessed negative, weighted by confidence."""
    checkable = [c for c in claim_assessments if c.get("checkable", True)]
    if not checkable:
        return {"risk": 0.0, "weight": 1.2, "detail": "no checkable claims extracted"}
    bad = {"CONTRADICTED": 1.0, "LIKELY_MISLEADING": 0.85, "MISLEADING": 0.8,
           "MIXED_EVIDENCE": 0.45, "OUTDATED": 0.4, "SATIRE/PARODY": 0.3}
    total = sum(float(c.get("confidence", 0.5)) * bad.get(c.get("verdict", "UNVERIFIED"), 0.05) for c in checkable)
    risk = min(1.0, total / len(checkable))
    counts: dict[str, int] = {}
    for c in checkable:
        counts[c.get("verdict", "?")] = counts.get(c.get("verdict", "?"), 0) + 1
    detail = "claim verdicts: " + (", ".join(f"{k}×{v}" for k, v in sorted(counts.items())) or "none")
    return {"risk": round(risk, 3), "weight": 1.6, "detail": detail}


def source_risk(evidence_scores: list[float]) -> dict[str, Any]:
    """Low-quality source reliance raises risk (quality 0..1 → inverted)."""
    if not evidence_scores:
        return {"risk": 0.5, "weight": 0.6, "detail": "no evidence retrieved — cannot assess source quality"}
    avg = sum(evidence_scores) / len(evidence_scores)
    detail = f"mean evidence-source quality {avg:.2f} across {len(evidence_scores)} items"
    return {"risk": round(max(0.0, 1.0 - avg), 3), "weight": 0.9, "detail": detail}


def evidence_conflict_risk(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    """Contradicted/mixed share of checkable claims (spec #39)."""
    checkable = [c for c in assessments if c.get("checkable", True)]
    if not checkable:
        return {"risk": 0.0, "weight": 1.0, "detail": "no checkable claims"}
    conflict = sum(1 for c in checkable if c.get("verdict") in ("CONTRADICTED", "MIXED_EVIDENCE", "LIKELY_MISLEADING", "MISLEADING"))
    detail = f"{conflict}/{len(checkable)} checkable claims face contradicting or mixed evidence"
    return {"risk": round(conflict / len(checkable), 3), "weight": 1.2, "detail": detail}


def media_risk(media: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Image/document forensics risk."""
    if not media:
        return {"risk": 0.0, "weight": 0.7, "detail": "no media to analyse"}
    manip = float(media.get("manipulation_risk") or 0.0)
    ai_sig = float(media.get("ai_generation_signal") or 0.0)
    forensics = media.get("forensics") or []
    doc_flag = min(0.4, 0.1 * len(forensics))
    risk = min(1.0, manip * 0.8 + ai_sig * 0.35 + doc_flag)
    detail = (f"image manipulation signals {manip:.2f}, ai-generation signal {ai_sig:.2f} "
              f"(model-based only), {len(forensics)} document forensic notes")
    return {"risk": round(risk, 3), "weight": 1.1, "detail": detail}


def anomaly_risk(text_stats: dict[str, Any], claim_count: int) -> dict[str, Any]:
    """Statistical oddities: ALL-CAPS bursts, extreme punctuation, thin sourcing."""
    detail_bits: list[str] = []
    risk = 0.0
    words = float(text_stats.get("words") or 0)
    if words:
        exclam = text_stats.get("exclamations", 0)
        caps_ratio = text_stats.get("caps_ratio", 0.0)
        if caps_ratio > 0.25:
            risk += 0.15
            detail_bits.append(f"high ALL-CAPS ratio ({caps_ratio:.0%})")
        if exclam >= 6:
            risk += 0.1
            detail_bits.append(f"{exclam} exclamation marks")
    if words and words < 40 and claim_count == 0:
        risk += 0.1
        detail_bits.append("very short content with no extractable claims")
    detail = "; ".join(detail_bits) or "no statistical anomalies"
    return {"risk": round(min(1.0, risk), 3), "weight": 0.6, "detail": detail}


def propagation_risk(spread_score: Optional[float]) -> dict[str, Any]:
    """Bridges to the forecasting engine (spec #30-#32). None → neutral."""
    if spread_score is None:
        return {"risk": 0.0, "weight": 0.0, "detail": "no propagation data for this content"}
    return {"risk": round(min(1.0, float(spread_score)), 3), "weight": 1.0,
            "detail": f"forecast-based spread risk {float(spread_score):.2f}"}


def numerical_risk(checks: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    """Deterministic numerical inconsistencies are strong misinformation
    signals (spec #23). Only Python arithmetic feeds this — never an LLM."""
    if not checks:
        return {"risk": 0.0, "weight": 0.0, "detail": "no numerical checks ran"}
    inconsistent = [c for c in checks if c.get("status") == "inconsistent"]
    total = len(checks)
    share = len(inconsistent) / max(1, total)
    risk = min(1.0, share * 1.6)
    if inconsistent:
        detail = f"{len(inconsistent)}/{total} deterministic arithmetic checks FAILED: " + \
                 "; ".join((c.get('detail') or '')[:80] for c in inconsistent[:2])
    else:
        detail = f"all {total} numerical checks passed (deterministic)"
    return {"risk": round(risk, 3), "weight": 1.3, "detail": detail}


def fuse(components: list[dict[str, Any]]) -> dict[str, Any]:
    """Weighted fusion → risk 0-100, confidence 0-100, level."""
    total_w = sum(c["weight"] for c in components if c["weight"] > 0)
    if total_w <= 0:
        return {"misinformation_risk": 0.0, "confidence": 30.0, "risk_level": "LOW",
                "components": components, "explanation": "no signal available"}
    score = sum(c["risk"] * c["weight"] for c in components) / total_w
    # confidence: more active (nonzero-weight) signals with agreement → higher
    active = [c for c in components if c["weight"] > 0]
    n_active = len(active)
    spread = max((c["risk"] for c in active), default=0.0) - min((c["risk"] for c in active), default=0.0)
    confidence = min(0.9, 0.35 + 0.08 * n_active - 0.15 * (spread > 0.6))
    explanation = " | ".join(f"{c['detail']} (risk {c['risk']:.2f}, weight {c['weight']})" for c in active)
    return {
        "misinformation_risk": round(score * 100.0, 1),
        "confidence": round(confidence * 100.0, 1),
        "risk_level": _level(score),
        "components": components,
        "explanation": explanation,
    }


def assess_misinformation_risk(
    text: str,
    text_stats: dict[str, Any],
    claim_assessments: list[dict[str, Any]],
    evidence_qualities: list[float],
    media: Optional[dict[str, Any]],
    spread_score: Optional[float],
    numerical_checks: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Full modular assessment (spec #33)."""
    components = [
        text_risk(text),
        claim_risk(claim_assessments),
        source_risk(evidence_qualities),
        evidence_conflict_risk(claim_assessments),
        media_risk(media),
        numerical_risk(numerical_checks),
        anomaly_risk(text_stats, len(claim_assessments)),
        propagation_risk(spread_score),
    ]
    return fuse(components)
