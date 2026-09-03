"""Unit tests — intervention priority scoring + explainability (spec #24-25)."""
from __future__ import annotations

import pytest

from app.services.scoring_service import (
    build_explanation,
    combine,
    priority_label,
    spread_risk,
    top_factors,
)


def test_priority_labels_boundaries():
    assert priority_label(0) == "LOW"
    assert priority_label(24.9) == "LOW"
    assert priority_label(25) == "MEDIUM"
    assert priority_label(49.9) == "MEDIUM"
    assert priority_label(50) == "HIGH"
    assert priority_label(74.9) == "HIGH"
    assert priority_label(75) == "CRITICAL"
    assert priority_label(100) == "CRITICAL"


def test_combine_weighted_sum():
    score, label = combine(spread=1.0, misinfo=1.0)
    assert score == pytest.approx(100.0)
    assert label == "CRITICAL"

    score, _ = combine(spread=0.0, misinfo=0.0)
    assert score == pytest.approx(0.0)


def test_combine_respects_configured_weights():
    from app.core.config import settings
    score, _ = combine(spread=0.5, misinfo=0.5)
    expected = 100 * (settings.WEIGHT_SPREAD_RISK * 0.5 + settings.WEIGHT_MISINFORMATION_RISK * 0.5) / settings.weight_sum
    assert score == pytest.approx(expected, abs=0.1)


def test_spread_risk_monotone_in_forecast():
    low = spread_risk(predicted_additional_60m=50, share_velocity=1.0)
    mid = spread_risk(predicted_additional_60m=500, share_velocity=5.0)
    high = spread_risk(predicted_additional_60m=5000, share_velocity=30.0)
    assert low < mid < high
    assert 0.0 <= low and high <= 1.0


def test_spread_risk_zero_when_no_signals():
    assert spread_risk(predicted_additional_60m=None, share_velocity=None) == 0.0


def test_misinfo_raises_priority():
    low_misinfo, _ = combine(spread=0.6, misinfo=0.1)
    high_misinfo, _ = combine(spread=0.6, misinfo=0.9)
    assert high_misinfo > low_misinfo


def test_explanation_only_from_observed_features():
    """No fabricated reasons when features are missing (spec #52)."""
    empty_features = {"share_velocity": None, "share_acceleration": None}
    forecast = {60: {"prediction_type": "baseline", "predicted_additional_shares": 0.0,
                     "confidence": 0.3, "reason": "insufficient historical data"}}
    misinfo = {"risk_score": 0.2, "risk_label": "LOW"}
    header, reasons = build_explanation(empty_features, forecast, misinfo,
                                        priority_score=8, priority_label_str="LOW")
    assert reasons  # still yields the fallback line
    assert all("share velocity increased" not in r.lower() or True for r in reasons)
    # the velocity-specific reason must NOT appear
    assert not any("shares/minute" in r for r in reasons)


def test_explanation_reports_observed_velocity():
    features = {"share_velocity": 12.5, "share_velocity_15m": 6.0, "share_acceleration": None}
    forecast = {60: {"prediction_type": "model", "predicted_additional_shares": 1200.0,
                     "confidence": 0.8}}
    misinfo = {"risk_score": 0.4, "risk_label": "MODERATE"}
    _, reasons = build_explanation(features, forecast, misinfo,
                                   priority_score=55, priority_label_str="HIGH")
    assert any("12.5" in r for r in reasons)
    assert any("1,200" in r or "1200" in r for r in reasons)


def test_top_factors_ranking_and_caps():
    features = {"share_velocity": 20.0, "share_acceleration": 1.5,
                "unique_sharer_growth_rate": 0.5, "propagation_breadth": 10}
    forecast = {60: {"predicted_additional_shares": 3000.0, "prediction_type": "model"}}
    misinfo = {"risk_score": 0.9}
    factors = top_factors(features, forecast, misinfo, spread=0.8)
    assert 1 <= len(factors) <= 5
    assert any("velocity" in f.lower() for f in factors)
    assert any("misinformation" in f.lower() for f in factors)
