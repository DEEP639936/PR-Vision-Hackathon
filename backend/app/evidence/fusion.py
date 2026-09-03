"""Evidence fusion + verdict taxonomy (spec #34, #35, #36, #39, #40).

No single model decides anything. Individual signals (claim model, fact-check
evidence, web evidence, source quality, temporal context) are kept separate,
fused transparently, and every verdict ships with its reasoning chain.

Verdict taxonomy (spec #35):
    SUPPORTED | LIKELY_SUPPORTED | UNVERIFIED | MIXED_EVIDENCE | MISLEADING |
    LIKELY_MISLEADING | CONTRADICTED | SATIRE_PARODY | OUTDATED |
    INSUFFICIENT_EVIDENCE
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from app.evidence.ranking import (
    RankingOutcome,
    SourceSignals,
    host_independence_key,
    rank_source,
    similarity_cluster_key,
)
from app.core.logging import get_logger

logger = get_logger("prvision.evidence.fusion")

VERDICTS = (
    "SUPPORTED", "LIKELY_SUPPORTED", "UNVERIFIED", "MIXED_EVIDENCE",
    "MISLEADING", "LIKELY_MISLEADING", "CONTRADICTED", "SATIRE/PARODY",
    "OUTDATED", "INSUFFICIENT EVIDENCE",
)

NEGATION_CUES = re.compile(
    r"\b(no|not|never|denied|denies|deny|false|falsehood|debunk|debunked|"
    r"refut(?:e|es|ed)|misleading|incorrect|wrong|unfounded|baseless|"
    r"no evidence|lacks? evidence|contrary|despite claims|rumou?rs?|hoax|"
    r"fabricated|doctored|falsely)\b",
    re.IGNORECASE,
)
_SUPPORT_CUES = re.compile(
    r"\b(confirm(?:s|ed)?|verified|prove[ds]?|according to|documented|"
    r"official(?:ly)? (?:confirm|stated|announced)|consistent with|in line with|"
    r"data shows|records show|corroborat(?:es|ed)|matches)\b",
    re.IGNORECASE,
)


@dataclass
class ScoredEvidence:
    """One evidence item after stance + quality scoring."""
    provider: str
    url: Optional[str]
    title: Optional[str]
    snippet: Optional[str]
    publisher: Optional[str]
    published_at: Optional[str]
    stance: str                       # supports|contradicts|neutral
    stance_confidence: float
    relevance: float
    quality: float
    ranking: RankingOutcome
    independence_key: str = ""
    cluster_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "url": self.url, "title": self.title,
            "snippet": self.snippet, "publisher": self.publisher,
            "published_at": self.published_at, "stance": self.stance,
            "stance_confidence": round(self.stance_confidence, 3),
            "relevance": round(self.relevance, 3), "quality": round(self.quality, 3),
            "classification": self.ranking.classification,
            "signals": self.ranking.signals,
            "independence_key": self.independence_key,
            "cluster_key": self.cluster_key,
        }


@dataclass
class ClaimAssessment:
    verdict: str
    confidence: float
    confidence_rationale: str
    supporting: int
    contradicting: int
    neutral: int
    primary_source_available: bool
    temporal_flag: Optional[str]
    explanation: str
    evidence: list[ScoredEvidence] = field(default_factory=list)
    fact_checks: list[dict] = field(default_factory=list)
    fused_signals: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------- stance
def classify_stance(claim_text: str, evidence_text: str) -> tuple[str, float]:
    """Lexical stance classifier: does evidence support or contradict the claim?

    Transparent heuristics first; designed so an LLM pass can later upgrade
    stance_confidence. Honesty rule: low lexical signal → neutral.
    """
    claim_terms = _content_terms(claim_text)
    ev_terms = _content_terms(evidence_text)
    overlap = len(claim_terms & ev_terms) / max(1, min(len(claim_terms), 8))
    if overlap < 0.15:
        return "neutral", 0.5

    neg_hits = len(NEGATION_CUES.findall(evidence_text))
    sup_hits = len(_SUPPORT_CUES.findall(evidence_text))
    # negation *about the same entities* is the contradiction signal
    if neg_hits and sup_hits == 0 and overlap >= 0.25:
        return "contradicts", min(0.85, 0.5 + 0.12 * neg_hits + overlap * 0.3)
    if neg_hits > sup_hits and overlap >= 0.25:
        return "contradicts", min(0.8, 0.45 + 0.1 * neg_hits)
    if sup_hits and overlap >= 0.2:
        return "supports", min(0.85, 0.5 + 0.1 * sup_hits + overlap * 0.3)
    if overlap >= 0.4:
        return "supports", 0.55   # same subject matter, no dispute language
    return "neutral", 0.5


def _content_terms(text: str) -> set[str]:
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of",
        "in", "on", "at", "for", "and", "or", "that", "this", "it", "as", "with",
        "has", "have", "had", "by", "from", "will", "would", "could", "should",
        "their", "they", "its", "his", "her", "our", "your", "not", "no", "new",
    }
    return {w for w in re.findall(r"[a-zà-ÿ0-9']{3,}", (text or "").lower()) if w not in stop}


# ---------------------------------------------------------------- temporal
def temporal_flag(claim_time: Optional[str], evidence_dates: list[Optional[str]]) -> Optional[str]:
    """Spec #12 — a claim true in 2022 may be false in 2026."""
    if not claim_time:
        return None
    ct = _year_of(claim_time)
    if ct is None:
        return None
    years = [y for y in (_year_of(d) for d in evidence_dates if d) if y is not None]
    if not years:
        return "UNSURE"
    newest = max(years)
    if newest and ct and newest - ct >= 2:
        return "OUTDATED"
    return "CURRENT"


def _year_of(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


# ---------------------------------------------------------------- fusion
def fuse_claim(
    claim_text: str,
    claim_type: str,
    claim_time: Optional[str],
    raw_evidence: list[dict[str, Any]],
    fact_checks: list[dict[str, Any]],
) -> ClaimAssessment:
    """Fuse evidence for ONE claim into a verdict (spec #34).

    raw_evidence items: {provider,url,title,snippet,publisher,published_at,relevance}
    fact_checks items:  {claim_text,textual_rating,publisher,published_at,url,snippet}
    """
    # ---- score & rank each evidence item ------------------------------
    scored: list[ScoredEvidence] = []
    fingerprints: dict[str, str] = {}
    host_seen: set[str] = set()
    for ev in raw_evidence:
        text_probe = " ".join(filter(None, [ev.get("title"), ev.get("snippet")]))[:1200]
        stance, stance_conf = classify_stance(claim_text, text_probe)
        signals = SourceSignals(
            host=_host_of(ev.get("url")) or (ev.get("publisher") or "unknown"),
            publisher=ev.get("publisher"),
            published_at=ev.get("published_at"),
            snippet=ev.get("snippet"),
            title=ev.get("title"),
            url=ev.get("url"),
        )
        ranking = rank_source(signals)
        ikey = host_independence_key(ev.get("url"))
        ckey = similarity_cluster_key(text_probe, fingerprints)
        fingerprints.setdefault(ckey, text_probe)
        scored.append(ScoredEvidence(
            provider=ev.get("provider", "web"),
            url=ev.get("url"),
            title=ev.get("title"),
            snippet=(ev.get("snippet") or "")[:600],
            publisher=ev.get("publisher"),
            published_at=ev.get("published_at"),
            stance=stance,
            stance_confidence=stance_conf,
            relevance=float(ev.get("relevance") or 0.4),
            quality=ranking.quality,
            ranking=ranking,
            independence_key=ikey,
            cluster_key=ckey,
        ))

    # ---- fact-check matches (strongest external signal, spec #7) -------
    fc_support = fc_contra = 0
    fc_notes: list[str] = []
    FC_NEG = re.compile(r"\b(false|falsehood|misleading|debunk|incorrect|unfounded|baseless|"
                        r"mostly false|pants on fire|fabricated|no evidence|lacks? evidence|hoax)\b", re.IGNORECASE)
    FC_TRUE = re.compile(r"\b(true|accurate|correct|mostly true|confirmed|supported)\b", re.IGNORECASE)
    FC_MIX = re.compile(r"\b(mixture|misleading|half|partly|out of context|missing context|"
                        r"exaggerat\w+|cherry-?pick\w*)\b", re.IGNORECASE)
    for fc in fact_checks:
        rating = fc.get("textual_rating") or ""
        if FC_MIX.search(rating):
            fc_contra += 1
            fc_notes.append(f"{fc.get('publisher') or 'Fact-checker'}: “{rating}”")
        elif FC_NEG.search(rating):
            fc_contra += 1
            fc_notes.append(f"{fc.get('publisher') or 'Fact-checker'}: “{rating}”")
        elif FC_TRUE.search(rating):
            fc_support += 1
            fc_notes.append(f"{fc.get('publisher') or 'Fact-checker'}: “{rating}”")

    # ---- independence-aware consensus (spec #40, #41) -------------------
    support_w = contra_w = neutral_w = 0.0
    indep_sup_hosts: set[str] = set()
    indep_con_hosts: set[str] = set()
    for s in scored:
        # weight = quality × relevance × stance_confidence, de-duplicated per cluster
        w = s.quality * (0.5 + 0.5 * s.relevance) * (0.5 + 0.5 * s.stance_confidence)
        if s.stance == "supports":
            support_w += w
            indep_sup_hosts.add(s.independence_key)
        elif s.stance == "contradicts":
            contra_w += w
            indep_con_hosts.add(s.independence_key)
        else:
            neutral_w += w * 0.3

    supporting = sum(1 for s in scored if s.stance == "supports")
    contradicting = sum(1 for s in scored if s.stance == "contradicts")
    neutral = sum(1 for s in scored if s.stance == "neutral")

    # ---- fused signal vector -------------------------------------------
    total = support_w + contra_w + neutral_w + 1e-6
    fused_signals = {
        "web_support_weight": round(support_w / total, 3),
        "web_contradict_weight": round(contra_w / total, 3),
        "independent_support_hosts": len(indep_sup_hosts),
        "independent_contradict_hosts": len(indep_con_hosts),
        "factcheck_support": fc_support,
        "factcheck_contradict": fc_contra,
        "fact_check_available": bool(fact_checks),
        "evidence_count": len(scored),
    }

    # ---- verdict decision table (evidence-first, conservative) ---------
    strong_contra = contra_w > support_w * 1.6 and (contra_w > 0.6 or fc_contra > 0)
    strong_support = support_w > contra_w * 1.6 and support_w > 0.8
    any_contra = contra_w > 0 or fc_contra > 0
    any_support = support_w > 0 or fc_support > 0

    if claim_type == "SATIRE":
        verdict = "SATIRE/PARODY"
    elif strong_contra and (fc_contra > 0 or len(indep_con_hosts) >= 2):
        verdict = "CONTRADICTED"
    elif strong_contra:
        verdict = "LIKELY_MISLEADING"
    elif fc_contra > 0 and any_support and fc_support == 0:
        verdict = "MIXED_EVIDENCE"
    elif FC_MIX_flag(fc_notes) and any_support:
        verdict = "MISLEADING"
    elif strong_support and (fc_support > 0 or len(indep_sup_hosts) >= 2):
        verdict = "SUPPORTED"
    elif strong_support:
        verdict = "LIKELY_SUPPORTED"
    elif any_support and any_contra:
        verdict = "MIXED_EVIDENCE"
    elif len(scored) == 0 and not fact_checks:
        verdict = "INSUFFICIENT EVIDENCE"
    elif not claim_type.startswith("FACTUAL"):
        verdict = "UNVERIFIED"
    else:
        verdict = "UNVERIFIED"

    # temporal downgrade (spec #12)
    tflag = temporal_flag(claim_time, [s.published_at for s in scored] + [fc.get("published_at") for fc in fact_checks])
    if tflag == "OUTDATED" and verdict in ("SUPPORTED", "LIKELY_SUPPORTED"):
        verdict = "OUTDATED"
    elif tflag == "OUTDATED" and verdict in ("MISLEADING", "LIKELY_MISLEADING", "CONTRADICTED"):
        verdict = verdict  # contradiction already accounts for staleness

    # non-checkable claims cannot be contradicted by evidence design
    if claim_type in ("OPINION", "QUESTION", "EMOTIONAL") and verdict not in ("SATIRE/PARODY",):
        verdict = "UNVERIFIED" if verdict in ("SUPPORTED", "LIKELY_SUPPORTED", "MISLEADING", "LIKELY_MISLEADING", "CONTRADICTED") else verdict

    # ---- confidence with rationale (spec #36) ---------------------------
    confidence, conf_note = _confidence(
        verdict, len(scored), len(indep_sup_hosts | indep_con_hosts), bool(fact_checks),
        disagreement=(support_w > 0 and contra_w > 0),
    )

    primary = any(re.search(r"\b(gov|\.int|\.edu|arxiv|nature|science|who\.int|un\.org|oecd)\b", s.url or "", re.IGNORECASE)
                  or s.ranking.classification in ("official", "academic") for s in scored)

    explanation = _explain(
        verdict, claim_text, supporting, contradicting, neutral, indep_sup_hosts, indep_con_hosts,
        fact_checks, fc_notes, tflag, claim_type, conf_note,
    )

    return ClaimAssessment(
        verdict=verdict,
        confidence=confidence,
        confidence_rationale=conf_note,
        supporting=supporting,
        contradicting=contradicting,
        neutral=neutral,
        primary_source_available=primary,
        temporal_flag=tflag,
        explanation=explanation,
        evidence=scored,
        fact_checks=fact_checks,
        fused_signals=fused_signals,
    )


def FC_MIX_flag(fc_notes: list[str]) -> bool:
    return any(re.search(r"(mixture|misleading|out of context|missing context|exaggerat|cherry)", n, re.IGNORECASE) for n in fc_notes)


def _confidence(verdict: str, n_evidence: int, n_indep: int, has_fc: bool, disagreement: bool) -> tuple[float, str]:
    base = 0.35
    notes: list[str] = []
    if n_evidence >= 6:
        base += 0.2
        notes.append(f"{n_evidence} evidence items retrieved")
    elif n_evidence >= 3:
        base += 0.12
        notes.append(f"moderate evidence pool ({n_evidence} items)")
    else:
        notes.append("sparse evidence pool — confidence reduced")
    if n_indep >= 3:
        base += 0.15
        notes.append(f"{n_indep} independent hosts")
    elif n_indep >= 2:
        base += 0.08
    else:
        notes.append("few independent sources — syndication risk")
    if has_fc:
        base += 0.2
        notes.append("professional fact-check evidence available")
    else:
        notes.append("no external fact-check matched (absence of a fact-check is NOT proof either way)")
    if disagreement:
        base -= 0.15
        notes.append("sources disagree — confidence reduced")
    if verdict in ("INSUFFICIENT EVIDENCE", "UNVERIFIED"):
        base = min(base, 0.55)
    confidence = max(0.15, min(0.95, base))
    return round(confidence, 3), "; ".join(notes)


def _explain(verdict, claim_text, sup, con, neu, sup_hosts, con_hosts, fcs, fc_notes, tflag, ctype, conf_note) -> str:
    parts: list[str] = []
    parts.append(f"Assessed as a {ctype.lower()} claim: “{(claim_text or '')[:180]}”.")
    parts.append(f"Retrieved evidence stance: {sup} supporting, {con} contradicting, {neu} neutral, "
                 f"across {len(set(sup_hosts) | set(con_hosts))} independent hosts.")
    if fcs:
        parts.append("External fact-checks: " + " | ".join(fc_notes[:3]) + "." if fc_notes else "")
    else:
        parts.append("No matching external fact-check found — this does NOT mean the claim is true.")
    if tflag == "OUTDATED":
        parts.append("Temporal check: evidence is substantially newer than the claim's stated time — treat as possibly outdated.")
    parts.append(f"Verdict {verdict}. Confidence reasoning: {conf_note}.")
    return " ".join(p for p in parts if p)


def _host_of(url: Optional[str]) -> Optional[str]:
    from urllib.parse import urlparse
    try:
        return (urlparse(url or "").hostname or "").lower().removeprefix("www.") or None
    except ValueError:
        return None
