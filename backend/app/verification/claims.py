"""Claim extraction engine (spec #10).

Breaks content into discrete factual claims instead of one binary label
(spec #11 principle: claim-by-claim verification).

Approach:
  1. Deterministic segmentation + cue-phrase heuristics (always available,
     no external dependency, reproducible).
  2. Optional LLM assist through the provider sidecar (hybrid mode) — used
     only to *refine typing/entities*; heuristic results remain the floor.

Claim types: FACTUAL | OPINION | PREDICTION | QUESTION | SATIRE | EMOTIONAL
Every claim carries: time context, entities, numeric facts, confidence.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.verify.claims")

CLAIM_TYPES = ("FACTUAL", "OPINION", "PREDICTION", "QUESTION", "SATIRE", "EMOTIONAL")

# --------------------------------------------------------------------- patterns
_CUE_ASSERT = re.compile(
    r"\b(confirm|confirmed|confirms|prove[ds]?|proven|announce[d]?|reveal(?:ed)?|report(?:ed)?|"
    r"according to|study finds|researchers|officials say|documents show|data shows|"
    r"facts? about|fact check|expos[eé]d|leaked|banned|declared|approved|discovered|"
    r"breakthrough|results show|survey found|statistics show)\b",
    re.IGNORECASE,
)
_CUE_OPINION = re.compile(
    r"\b(i (?:think|believe|feel|guess)|in my (?:opinion|view)|arguably|personally|"
    r"seems? (?:like|to)|probably|perhaps|maybe|in my experience|"
    r"best|worst|amazing|terrible|beautiful|ugly|overrated|underrated)\b",
    re.IGNORECASE,
)
_CUE_PREDICTION = re.compile(
    r"\b(will (?:be|become|rise|fall|win|lose|happen|announce)|going to|expected to|"
    r"forecasts?|predict(?:s|ed|ion)?|by \d{4}|in \d+ years|soon|plan(?:s|ned)? to|"
    r"projected to|likely to)\b",
    re.IGNORECASE,
)
_CUE_QUESTION = re.compile(
    r"\?\s*$|^(is|are|was|were|do|does|did|can|could|should|would|will|has|have|who|what|when|where|why|how)\b",
    re.IGNORECASE,
)
_CUE_SATIRE = re.compile(
    r"\b(baboon(?:s)?|onion|parody|satire|obviously fake|in an alternate universe|"
    r"borat|the hard times|waterford whispers|newsbiscuit)\b",
    re.IGNORECASE,
)
_CUE_EMOTION = re.compile(
    r"\b(shocking!|unbelievable!|outrage(?:ous)?!|disgusting|heartbreaking|devastating|"
    r"you won'?t believe|insane!|terrifying|horrific|!!!+)\b",
    re.IGNORECASE,
)
_CUE_URGENCY = re.compile(
    r"\b(breaking|urgent|share (?:this )?(?:before|now)|before (?:it'?s|they) delet|"
    r"banned by media|censored|they don'?t want you to (?:know|see))\b",
    re.IGNORECASE,
)

_REL_TIME = re.compile(
    r"\b(today|yesterday|tomorrow|last (?:night|week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"this (?:morning|week|month|year)|next (?:week|month|year)|just now|hours? ago|days? ago|weeks? ago|years? ago)\b",
    re.IGNORECASE,
)
_ABS_DATE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}|\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,? \d{4})\b",
    re.IGNORECASE,
)
_NUM_FACT = re.compile(
    r"\b(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>%|percent|per\s?cent|million|billion|thousand|k|m|bn|km|kg|°c|°f|mph|km/h|degrees?)?\b",
    re.IGNORECASE,
)
# capitalized multi-word spans → candidate named entities (heuristic NER)
_ENTITY_SPAN = re.compile(
    r"\b([A-Z][a-zà-ÿ]+(?:[\s'-][A-Z][a-zà-ÿ]+){0,4}|[A-Z]{2,6})\b"
)

_STOPWORD_HEADS = {
    "The", "This", "That", "These", "Those", "There", "Here", "It", "Its",
    "A", "An", "And", "But", "However", "When", "While", "If", "In", "On",
    "At", "For", "With", "From", "By", "As", "He", "She", "They", "We", "You",
    "I", "His", "Her", "Their", "Our", "My", "Your", "What", "Why", "How",
    "Who", "Where", "Not", "No", "Yes", "So", "Then", "Than", "Also", "Now",
    "New", "First", "Last", "Next", "One", "Two", "It's", "According",
}
_ORG_HINTS = re.compile(
    r"\b(inc|corp|corporation|ltd|llc|gmbh|university|ministry|department|agency|"
    r"organization|organisation|institute|foundation|council|bureau|commission|"
    "association|federation|union|committee|who|un|nasa|eu|fbi|cia|cdc|nhs"
    r")\b\.?",
    re.IGNORECASE,
)


@dataclass
class ExtractedClaim:
    ordinal: int
    text: str
    claim_type: str
    checkable: bool
    confidence: float
    time_context: Optional[str] = None
    entities: list[dict[str, str]] = field(default_factory=list)
    numbers: list[dict[str, Any]] = field(default_factory=list)
    method: str = "heuristic"
    urgency_markers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "text": self.text,
            "claim_type": self.claim_type,
            "checkable": self.checkable,
            "claim_confidence": round(self.confidence, 3),
            "time_context": self.time_context,
            "entities": self.entities,
            "numbers": self.numbers,
            "extraction_method": self.method,
            "urgency_markers": self.urgency_markers,
        }


def split_sentences(text: str) -> list[str]:
    """Lightweight sentence segmentation robust to abbreviations we care about."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    protected = text.replace("e.g.", "e‹g›").replace("i.e.", "i‹e›").replace("etc.", "etc‹›").replace("Dr.", "Dr‹›").replace("vs.", "vs‹›")
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", protected)
    return [p.replace("‹g›", ".g.").replace("‹e›", ".e.").replace("‹›", ".").replace("‹›", ".").strip() for p in parts if p.strip()]


def _classify_type(sentence: str) -> tuple[str, float, bool]:
    """Return (claim_type, confidence, checkable)."""
    s = sentence.strip()
    if _CUE_SATIRE.search(s) and len(s) > 30:
        return "SATIRE", 0.55, False
    if _CUE_QUESTION.search(s):
        return "QUESTION", 0.85, False
    if _CUE_EMOTION.search(s) and not _CUE_ASSERT.search(s):
        return "EMOTIONAL", 0.6, False
    if _CUE_OPINION.search(s) and not _CUE_ASSERT.search(s):
        return "OPINION", 0.7, False
    if _CUE_PREDICTION.search(s):
        return "PREDICTION", 0.65, True
    if _CUE_ASSERT.search(s):
        return "FACTUAL", min(0.92, 0.6 + 0.08 * len(_CUE_ASSERT.findall(s))), True
    # contains specific numbers/named entities + date-like anchoring → likely factual
    has_num = bool(_NUM_FACT.search(s))
    has_ent = bool(_ENTITY_SPAN.search(s[1:] if s and s[0].isupper() else s))
    if has_num and has_ent:
        return "FACTUAL", 0.58, True
    if has_num:
        return "FACTUAL", 0.52, True
    return "OPINION", 0.4, False


def _extract_entities(sentence: str) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in _ENTITY_SPAN.finditer(sentence):
        name = m.group(1).strip(" .,;:!?\"'()")
        if not name or name in seen:
            continue
        head = name.split()[0]
        if head in _STOPWORD_HEADS and not name.isupper():
            continue
        if name.isdigit():
            continue
        kind = "ORGANIZATION" if _ORG_HINTS.search(name) else ("PERSON" if name.split()[0].isupper() and len(name.split()) <= 4 else "ENTITY")
        seen.add(name)
        entities.append({"name": name, "type": kind})
        if len(entities) >= 8:
            break
    return entities


def _extract_time(sentence: str) -> Optional[str]:
    m = _REL_TIME.search(sentence)
    if m:
        return m.group(0).lower()
    m = _ABS_DATE.search(sentence)
    if m:
        return m.group(1)
    year = re.search(r"\b(19|20)\d{2}\b", sentence)
    return year.group(0) if year else None


def _extract_numbers(sentence: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _NUM_FACT.finditer(sentence):
        raw = m.group("value")
        unit = (m.group("unit") or "").lower().replace("percent", "%").replace("per cent", "%")
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if unit in ("m",):
            unit, value = "million", value * 1_000_000
        elif unit in ("bn", "billion"):
            unit, value = "billion", value * 1_000_000_000
        elif unit in ("k", "thousand"):
            unit, value = "thousand", value * 1_000
        out.append({"raw": m.group(0), "value": value, "unit": unit or None, "position": m.start()})
        if len(out) >= 10:
            break
    return out


def _urgency_markers(sentence: str) -> list[str]:
    markers: list[str] = []
    if _CUE_URGENCY.search(sentence):
        markers.extend(m.group(0).lower() for m in _CUE_URGENCY.finditer(sentence))
    return markers


def heuristic_extract(text: str, max_claims: Optional[int] = None) -> list[ExtractedClaim]:
    """Deterministic claim extraction. Always available."""
    max_claims = max_claims or settings.VERIFY_MAX_CLAIMS
    claims: list[ExtractedClaim] = []
    ordinal = 0
    for sentence in split_sentences(text):
        if len(sentence.split()) < 5:
            continue
        claim_type, conf, checkable = _classify_type(sentence)
        claim = ExtractedClaim(
            ordinal=ordinal + 1,
            text=sentence[:2000],
            claim_type=claim_type,
            checkable=checkable,
            confidence=conf,
            time_context=_extract_time(sentence),
            entities=_extract_entities(sentence),
            numbers=_extract_numbers(sentence),
            urgency_markers=_urgency_markers(sentence),
        )
        claims.append(claim)
        ordinal += 1
        if ordinal >= max_claims:
            break
    return claims


# ------------------------------------------------------------------ LLM assist
_LLM_SYSTEM = (
    "You are a precise claim-analysis engine for a misinformation verification platform. "
    "For EACH numbered sentence return STRICT JSON: {\"claims\":[{\"n\":1,\"type\":\"FACTUAL|OPINION|"
    "PREDICTION|QUESTION|SATIRE|EMOTIONAL\",\"checkable\":true|false,\"entities\":[{\"name\":\"...\","
    "\"type\":\"PERSON|ORGANIZATION|PLACE|EVENT|PRODUCT|OTHER\"}],\"time_context\":\"...\" or null}]}. "
    "Never invent claims; only classify the sentences given. Return JSON only."
)


async def llm_refine(text: str, sidecar_url: str, timeout: float) -> Optional[list[dict[str, Any]]]:
    """Ask the sidecar LLM to refine typing/entities. Returns raw LLM claim list or None."""
    sentences = split_sentences(text)[: settings.VERIFY_MAX_CLAIMS]
    if not sentences:
        return None
    numbered = "\n".join(f"{i+1}. {s[:400]}" for i, s in enumerate(sentences))
    payload = {"system": _LLM_SYSTEM, "prompt": numbered, "max_tokens": 1600}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{sidecar_url.rstrip('/')}/llm", json=payload)
            data = resp.json()
        if not data.get("ok"):
            logger.info("LLM refine unavailable: %s", data.get("error"))
            return None
        raw = (data.get("data") or {}).get("text", "")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        parsed = json.loads(match.group(0))
        claims = parsed.get("claims")
        return claims if isinstance(claims, list) else None
    except Exception as exc:  # network/JSON issues → heuristic floor stands
        logger.info("LLM refine skipped (%s)", exc.__class__.__name__)
        return None


def merge_llm(claims: list[ExtractedClaim], llm_claims: list[dict[str, Any]]) -> list[ExtractedClaim]:
    """Merge LLM typing/entities into heuristic claims (hybrid mode)."""
    by_n = {}
    for c in llm_claims:
        try:
            by_n[int(c.get("n"))] = c
        except (TypeError, ValueError):
            continue
    for claim in claims:
        ref = by_n.get(claim.ordinal)
        if not ref:
            continue
        llm_type = str(ref.get("type", "")).upper()
        if llm_type in CLAIM_TYPES and llm_type != claim.claim_type:
            # LLM refines; keep checkable semantics aligned with type
            claim.claim_type = llm_type
            claim.checkable = llm_type in ("FACTUAL", "PREDICTION")
            claim.confidence = min(0.95, max(claim.confidence, 0.75))
            claim.method = "hybrid"
        llm_entities = ref.get("entities") or []
        names = {e["name"].lower() for e in claim.entities}
        for ent in llm_entities:
            if isinstance(ent, dict) and ent.get("name") and ent["name"].lower() not in names:
                claim.entities.append({"name": str(ent["name"])[:120], "type": str(ent.get("type", "OTHER")).upper()})
        if ref.get("time_context") and not claim.time_context:
            claim.time_context = str(ref["time_context"])[:120]
    return claims


async def extract_claims(text: str, use_llm: Optional[bool] = None) -> list[ExtractedClaim]:
    """Full extraction: heuristic floor + optional LLM refinement (spec #10)."""
    claims = heuristic_extract(text)
    if use_llm is None:
        use_llm = settings.SIDECAR_ENABLED
    if use_llm and claims:
        refined = await llm_refine(text, settings.SIDECAR_URL, settings.SIDECAR_TIMEOUT_SECONDS)
        if refined:
            claims = merge_llm(claims, refined)
    return claims
