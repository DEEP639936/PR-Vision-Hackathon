"""Source-quality ranking + provenance profiles (spec #8, #47).

Design principles:
  * Contextual, NOT TLD-dumb. A .gov page can host outdated or contested
    claims; a .io blog can be a primary source. Signals are weighed, and the
    FULL signal breakdown is returned so the UI can show *why* a score is
    what it is (spec #8 forbids simplistic domain logic).
  * Transparent: every contribution is recorded as {signal, effect, note}.
  * Profiles are learned per host over time (observation_count) but never
    become a permanent blacklist/whitelist (spec #47).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from app.core.logging import get_logger

logger = get_logger("prvision.evidence.ranking")

# ---------------------------------------------------------------------------
# Signal catalogue — each detector returns (effect: -1..+1, note) or None.
# Weights are small and additive; the total is squashed to 0..1.
# ---------------------------------------------------------------------------
@dataclass
class SourceSignals:
    host: str
    publisher: Optional[str] = None
    published_at: Optional[str] = None    # provider-supplied raw date
    snippet: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    cross_source_agreement: float = 0.0   # 0-1 from fusion layer
    is_fact_check_publisher: bool = False
    is_claimed_primary: bool = False      # evidence appears to quote documents/data directly
    extra: dict[str, Any] = field(default_factory=dict)


# --- known-fact-check outlets (reputation list, transparent + extendable) ---
KNOWN_FACT_CHECKERS = {
    "snopes.com", "factcheck.org", "politifact.com", "fullfact.org",
    "afp.com", "apnews.com", "reuters.com", "bbc.com", "aclu.org",
    "factcheck.afp.com", "usa.today", "checkyourfact.com", "leadstories.com",
    "truthormyth.com", "aap.com.au", "verifica.rai.it", "dpa-factchecking.com",
}

# Official / institutional identity signals (contextual indicators, not proof)
_OFFICIAL_HINTS = re.compile(
    r"(^|\.)(gov|gov\.uk|gov\.au|gouv\.fr|europa\.eu|un\.org|who\.int|worldbank\.org|"
    r"oecd\.org|nasa\.gov|noaa\.gov|ecb\.europa\.eu|fda\.gov|cdc\.gov|esa\.int)$"
)
_ACADEMIC_HINTS = re.compile(
    r"(\.edu$|\.ac\.[a-z]{2}$|edu\.au$|\.edu\.[a-z]{2}$|arxiv\.org$|nature\.com$|"
    r"science\.org$|sciencedirect\.com$|springer\.com$|jstor\.org$|plos\.org$|plos\.com$|nih\.gov$|ncbi\.nlm\.nih\.gov$)"
)
_MAJOR_NEWS = re.compile(
    r"(reuters\.com$|apnews\.com$|bbc\.(com|co\.uk)$|nytimes\.com$|theguardian\.com$|"
    r"washingtonpost\.com$|wsj\.com$|ft\.com$|economist\.com$|aljazeera\.com$|npr\.org$|"
    r"dw\.com$|france24\.com$|cnbc\.com$|bloomberg\.com$|axios\.com$|politico\.com$)"
)
_PLATFORM_HOSTS = re.compile(
    r"(twitter\.com$|x\.com$|facebook\.com$|instagram\.com$|reddit\.com$|"
    r"tiktok\.com$|youtube\.com$|linkedin\.com$|medium\.com$|substack\.com$)"
)
_CONTENT_FARM_HINTS = re.compile(
    r"(\.click$|\.top$|\.xyz$|\.buzz$|\.loan$|\.work$|\.life$|\.club$|\.online$|\.site$|"
    r"^\d|^(?:\d+[a-z-]+|[a-z]+-\d+)[a-z0-9-]*\.(?:com|net|org)$)"
)
_TRANSPARENCY_HINTS = re.compile(
    r"\b(about us|editorial (?:policy|standards)|contact|corrections|sourced|"
    r"references|according to|data|methodology)\b",
    re.IGNORECASE,
)
_SPONSORED_HINTS = re.compile(r"\b(sponsored|advertorial|promoted|affiliate|paid post)\b", re.IGNORECASE)


@dataclass
class RankingOutcome:
    quality: float                                   # 0-1
    signals: list[dict[str, Any]]                    # [{signal, effect, note}]
    classification: str                              # official|academic|news|fact_checker|social|blog|unknown...

    def to_signals_json(self) -> str:
        import json
        return json.dumps(self.signals, ensure_ascii=False)


def _squash(x: float) -> float:
    """Map unbounded additive score to 0..1 with 0.5 at neutral."""
    return 1 / (1 + pow(2.718281828, -2.2 * x))


def rank_source(sig: SourceSignals) -> RankingOutcome:
    """Contextual source-quality ranking. Returns quality 0..1 + full breakdown."""
    host = (sig.host or "").lower().removeprefix("www.")
    url_path = urlparse(sig.url or "").path.lower() if sig.url else ""
    score = 0.0
    signals: list[dict[str, Any]] = []

    def add(name: str, effect: float, note: str) -> None:
        nonlocal score
        score += effect
        signals.append({"signal": name, "effect": round(effect, 3), "note": note})

    # identity / classification signals
    if host in KNOWN_FACT_CHECKERS or sig.is_fact_check_publisher:
        add("fact_check_reputation", +0.9, "Recognised fact-checking organisation")
    if _OFFICIAL_HINTS.search(host):
        add("official_institution", +0.7, "Official / institutional domain — primary source indicator, not proof")
    if _ACADEMIC_HINTS.search(host):
        add("academic_publisher", +0.6, "Academic publisher or preprint host")
    if _MAJOR_NEWS.search(host):
        add("major_newsroom", +0.35, "Established newsroom with editorial standards")
    if _PLATFORM_HOSTS.search(host):
        add("social_platform", -0.25, "User-generated social platform content — verify the underlying account")
    if _CONTENT_FARM_HINTS.search(host) and not _MAJOR_NEWS.search(host):
        add("domain_risk_pattern", -0.5, "Domain pattern associated with low-quality networks (contextual, not conclusive)")

    # transparency / sourcing cues in the visible text
    text_probe = " ".join(filter(None, [sig.title, sig.snippet]))
    if text_probe:
        if _TRANSPARENCY_HINTS.search(text_probe):
            add("transparency_cues", +0.2, "Cites sources / explains methodology in the visible excerpt")
        if _SPONSORED_HINTS.search(text_probe):
            add("sponsored_content", -0.45, "Sponsored or promotional content marker")

    # primary-source proximity (quotes docs, data, officials)
    if sig.is_claimed_primary or re.search(r"\b(according to (?:the )?(?:official|data|documents)|statement|press release|dataset|filings?)\b", text_probe or "", re.IGNORECASE):
        add("primary_source_cue", +0.3, "References primary material (documents / data / statements)")

    # recency vs claim context — stale evidence is weaker (spec #12 temporal)
    if sig.published_at:
        dt = _loose_parse(sig.published_at)
        if dt:
            age_days = (datetime.now(timezone.utc) - dt).days
            if age_days <= 7:
                add("recency", +0.25, "Published within the last week")
            elif age_days > 365 * 3:
                add("stale_evidence", -0.2, f"Evidence is {age_days // 365}+ years old — check temporal context")

    # cross-source agreement (from fusion layer; independence-aware)
    if sig.cross_source_agreement:
        add("cross_source_agreement", +0.3 * sig.cross_source_agreement, "Consistent with other independent sources")

    # citation quality proxy: path depth & parameters (shallow canonical pages trend editorial)
    if url_path and url_path != "/":
        depth = url_path.strip("/").count("/") + 1
        if depth <= 3:
            add("editorial_url_structure", +0.05, "Editorial URL structure")

    quality = _squash(score)
    classification = _classify_host(host, sig)
    return RankingOutcome(quality=round(quality, 3), signals=signals, classification=classification)


def _classify_host(host: str, sig: SourceSignals) -> str:
    if host in KNOWN_FACT_CHECKERS or sig.is_fact_check_publisher:
        return "fact_checker"
    if _OFFICIAL_HINTS.search(host):
        return "official"
    if _ACADEMIC_HINTS.search(host):
        return "academic"
    if _MAJOR_NEWS.search(host):
        return "news"
    if _PLATFORM_HOSTS.search(host):
        return "social"
    if _CONTENT_FARM_HINTS.search(host):
        return "low_credibility_pattern"
    if re.search(r"\.(org)$", host):
        return "organization"
    if re.search(r"\.(com|net|io|co|news|blog|info)$", host):
        return "media_or_blog"
    return "unknown"


def _loose_parse(value: str) -> Optional[datetime]:
    v = (value or "").strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%d", "%Y-%m"):
        try:
            dt = datetime.strptime(v[:26], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------- independence
def host_independence_key(url: Optional[str]) -> str:
    """Cluster key: registrable-domain-ish host so syndicated copies collapse
    into one origin (spec #41 — 20 copies of one wire story ≠ 20 sources)."""
    host = (urlparse(url or "").hostname or "unknown").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in {"co", "com", "org", "gov", "ac", "net"}:
        host = ".".join(parts[-3:])
    elif len(parts) > 2:
        host = ".".join(parts[-2:])
    return host


def similarity_cluster_key(text: str, fingerprints: dict[str, str], threshold_chars: int = 90) -> str:
    """Group near-identical text (syndication / copy detection, spec #42).

    Uses a cheap character-trigram Jaccard against existing fingerprints.
    Returns the cluster key (first host seen) for this text.
    """
    norm = re.sub(r"\s+", " ", (text or "").lower()).strip()
    trigrams = {norm[i:i + 3] for i in range(max(0, len(norm) - 3))} if norm else set()
    for key, fp in fingerprints.items():
        fp_norm = re.sub(r"\s+", " ", fp.lower()).strip()
        fp_tri = {fp_norm[i:i + 3] for i in range(max(0, len(fp_norm) - 3))}
        if not trigrams or not fp_tri:
            continue
        jac = len(trigrams & fp_tri) / max(1, len(trigrams | fp_tri))
        if jac >= 0.55 and len(norm) >= threshold_chars:
            return key
    return norm[:48] or "empty"
