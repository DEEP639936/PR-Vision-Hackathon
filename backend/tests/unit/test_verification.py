"""Verification engine tests — claims, numerics, ranking, fusion, forensics."""
from __future__ import annotations

import pytest

from app.evidence.fusion import classify_stance, fuse_claim, temporal_flag
from app.evidence.ranking import (
    RankingOutcome, SourceSignals, host_independence_key, rank_source,
    similarity_cluster_key,
)
from app.media.numeric import (
    check_growth, check_percentages, check_table, check_totals, run_all_text_checks,
)
from app.verification.claims import heuristic_extract, split_sentences
from app.verification.ingestion import detect_input_kind, text_from_plain, validate_url, IngestionError


# --------------------------------------------------------------------- claims
def test_sentence_segmentation():
    sentences = split_sentences("Dr. Smith announced results. Growth rose 5%! Is it real?")
    assert len(sentences) == 3
    assert sentences[0].startswith("Dr. Smith")


def test_claim_typing_factual():
    claims = heuristic_extract("Scientists confirmed that the vaccine trial succeeded yesterday.")
    assert claims and claims[0].claim_type == "FACTUAL"
    assert claims[0].checkable is True
    assert claims[0].time_context == "yesterday"


def test_claim_typing_mixed():
    text = ("I think this movie is terrible. Markets will crash by 2027. "
            "The ministry announced 1,200 new jobs on Monday.")
    claims = heuristic_extract(text)
    types = [c.claim_type for c in claims]
    assert "OPINION" in types and "PREDICTION" in types
    factual = [c for c in claims if c.claim_type == "FACTUAL"]
    assert factual and factual[0].numbers


def test_claim_entities_and_numbers():
    claims = heuristic_extract("The WHO announced 48,768 cholera cases in Sudan on 2025-08-29.")
    c = claims[0]
    assert any(e["name"] == "WHO" for e in c.entities)
    assert any(n["value"] == 48768 for n in c.numbers)
    assert c.time_context


# ------------------------------------------------------------------- numeric
def test_percentage_bound_inconsistent():
    checks = check_percentages("150% of patients fully recovered before treatment started.")
    assert any(c.status == "inconsistent" for c in checks)


def test_growth_consistency_detection():
    checks = check_growth("Water access grew 450% from 100 units to 150 units last year.")
    assert checks and checks[0].status == "inconsistent"
    assert "50.0%" in checks[0].expected


def test_growth_consistency_match():
    checks = check_growth("Revenue grew 50% from 100 units to 150 units.")
    assert checks and checks[0].status == "consistent"


def test_totals_sum():
    ok = check_totals("48,768 cases and 1,094 deaths are 49,862 total.")
    assert ok and ok[0].status == "consistent"          # 48768 + 1094 = 49862 ✓
    bad = check_totals("48,768 cases and 1,094 deaths are 60,000 total.")
    assert bad and bad[0].status == "inconsistent"       # real sum is 49,862
    assert "49862" in bad[0].expected.replace(",", "")


def test_table_growth():
    checks = check_table([["100"], ["150"]], ["Units"])
    assert checks and "50.0%" in checks[0].detail


def test_run_all_battery_no_crash_on_empty():
    assert run_all_text_checks("") == []


# ------------------------------------------------------------------- ranking
def test_ranking_fact_checker_high_quality():
    out = rank_source(SourceSignals(host="snopes.com", snippet="debunked the claim"))
    assert out.quality > 0.75
    assert out.classification == "fact_checker"
    assert any(s["signal"] == "fact_check_reputation" for s in out.signals)
    # single-word .com domains must NOT be treated as content farms
    assert not any(s["signal"] == "domain_risk_pattern" for s in out.signals)


def test_ranking_official_domain():
    out = rank_source(SourceSignals(host="who.int", snippet="official statement"))
    assert out.classification == "official"
    assert any(s["signal"] == "official_institution" for s in out.signals)


def test_ranking_is_not_tld_dumb():
    """A .gov page with sponsored language must NOT score top quality (spec #8)."""
    out = rank_source(SourceSignals(host="example.gov", snippet="sponsored advertorial post"))
    assert out.quality < 0.85
    assert any(s["signal"] == "sponsored_content" for s in out.signals)


def test_independence_clustering():
    assert host_independence_key("https://www.bbc.co.uk/news/x") == "bbc.co.uk"
    assert host_independence_key("https://syndicated.news.example.com/a") == "example.com"


def test_similarity_cluster():
    fp = {}
    k1 = similarity_cluster_key("The government announced a new policy on climate change yesterday morning.", fp)
    fp[k1] = "The government announced a new policy on climate change yesterday morning."
    k2 = similarity_cluster_key("The government announced a new policy on climate change yesterday morning.", fp)
    assert k1 == k2
    k3 = similarity_cluster_key("Quantum computing breakthrough announced by completely different researchers in Tokyo.", fp)
    assert k3 != k1


# -------------------------------------------------------------------- fusion
def test_stance_contradiction():
    stance, conf = classify_stance(
        "The WHO announced 48,768 cholera cases in Sudan",
        "No evidence of 48,768 cholera cases; the WHO denied the reported numbers")
    assert stance == "contradicts"


def test_stance_support():
    stance, _ = classify_stance(
        "The WHO announced 48,768 cholera cases in Sudan",
        "According to the WHO, records show 48,768 cholera cases were reported in Sudan")
    assert stance == "supports"


def test_fuse_contradicted():
    assessment = fuse_claim(
        "Event X happened on Monday", "FACTUAL", "Monday",
        [{"provider": "web", "url": "https://a.gov/x", "title": "Officials deny event X on Monday",
          "snippet": "No evidence event X happened Monday; records show it did not happen",
          "publisher": "Gov", "published_at": "2026-08-01", "relevance": 0.9},
         {"provider": "web", "url": "https://b.org/y", "title": "Debunk: event X was refuted",
          "snippet": "The claim that event X happened Monday is false and unfounded",
          "publisher": "Org", "published_at": "2026-08-02", "relevance": 0.9}],
        [{"claim_text": "Event X Monday", "textual_rating": "False", "publisher": "Snopes",
          "published_at": "2026-08-03", "url": "https://snopes.com/z", "snippet": "review"}])
    assert assessment.verdict in ("CONTRADICTED", "LIKELY_MISLEADING")
    assert assessment.contradicting >= 2


def test_fuse_no_evidence_is_insufficient():
    assessment = fuse_claim("Some claim", "FACTUAL", None, [], [])
    assert assessment.verdict == "INSUFFICIENT EVIDENCE"
    assert assessment.confidence <= 0.55


def test_temporal_outdated():
    assert temporal_flag("2022", ["2026-01-01"]) == "OUTDATED"
    assert temporal_flag("2026", ["2026-08-01"]) == "CURRENT"
    assert temporal_flag("recently", []) is None


# ---------------------------------------------------------------- ingestion
def test_input_kind_detection():
    assert detect_input_kind("https://example.com/a") == "url"
    assert detect_input_kind("plain text about things") == "text"
    assert detect_input_kind("", "report.pdf") == "pdf"
    assert detect_input_kind("", "shot.png") == "image"


def test_validate_url_blocks_private_networks():
    with pytest.raises(IngestionError):
        validate_url("http://127.0.0.1:3000/admin")
    with pytest.raises(IngestionError):
        validate_url("http://192.168.1.10/")
    with pytest.raises(IngestionError):
        validate_url("http://localhost/x")


def test_text_normalization():
    content = text_from_plain("Word " * 30)
    assert content.text_stats["words"] == 30
    assert content.source_classification == "LIVE"
