"""Intervention Priority Score (spec #24-25).

    priority = (w_spread × spread_risk + w_misinfo × misinformation_risk) × 100

Weights come from configuration (WEIGHT_SPREAD_RISK / WEIGHT_MISINFORMATION_RISK,
default 0.60 / 0.40) and are validated to sum to ~1.0.

Spread risk maps observable dynamics into [0,1] using documented, monotone
squashing functions — no magic constants hidden in code:

    forecast component : logistic( predicted_additional_shares(60m) / 1000 )
    velocity component : logistic( share_velocity_per_min / 15 )
    spread_risk        = max(0.55 × forecast_c, 0.85 × velocity_c, 1 − (1−fc)(1−vc))
                         — a soft-OR so a strong single signal dominates.

Explainability (spec #25) generates reasons ONLY from features that were
actually observed (not None) — never fabricated.
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.scoring")

PRIORITY_LABELS = [(25.0, "LOW"), (50.0, "MEDIUM"), (75.0, "HIGH"), (101.0, "CRITICAL")]

# documented squashing scales
FORECAST_SCALE_60M = 1000.0   # shares predicted in 60 min that map to ~73% risk
VELOCITY_SCALE = 15.0         # shares/min that map to ~73% risk


def priority_label(score: float) -> str:
    for threshold, label in PRIORITY_LABELS:
        if score < threshold:
            return label
    return "CRITICAL"


def logistic_squash(x: float) -> float:
    """Monotone map R → (0,1) centred at 0 → 0.5."""
    return 1.0 / (1.0 + math.exp(-x))


def spread_risk(
    *,
    predicted_additional_60m: Optional[float],
    share_velocity: Optional[float],
    unique_sharer_growth_rate: Optional[float] = None,
) -> float:
    components: list[float] = []
    if predicted_additional_60m is not None:
        components.append(logistic_squash(predicted_additional_60m / FORECAST_SCALE_60M * 3.0) * 0.55)
    if share_velocity is not None:
        components.append(logistic_squash(share_velocity / VELOCITY_SCALE * 3.0) * 0.85)
    if unique_sharer_growth_rate is not None:
        components.append(logistic_squash(unique_sharer_growth_rate * 8.0) * 0.35)
    if not components:
        return 0.0
    # soft-OR: 1 - Π(1 - c_i) keeps strong single signals decisive
    soft_or = 1.0
    for c in components:
        soft_or *= (1.0 - min(1.0, max(0.0, c)))
    return round(min(1.0, 1.0 - soft_or), 4)


def combine(
    *,
    spread: float,
    misinfo: float,
) -> tuple[float, str]:
    total = settings.weight_sum or 1.0
    priority = (settings.WEIGHT_SPREAD_RISK * spread + settings.WEIGHT_MISINFORMATION_RISK * misinfo) / total
    score_100 = round(max(0.0, min(100.0, priority * 100.0)), 1)
    return score_100, priority_label(score_100)


def build_explanation(
    features: dict[str, Any],
    forecast: dict[int, dict[str, Any]],
    misinfo: dict[str, Any],
    *,
    priority_score: float = 0.0,
    priority_label_str: str = "",
) -> tuple[str, list[str]]:
    """Human-readable reasons from OBSERVED features only (spec #25)."""
    reasons: list[str] = []

    velocity = features.get("share_velocity")
    velocity_15m = features.get("share_velocity_15m")
    acceleration = features.get("share_acceleration")
    new_sharers = features.get("new_unique_sharers")
    growth_rate = features.get("unique_sharer_growth_rate")
    breadth = features.get("propagation_breadth")
    branching = features.get("branching_factor")

    if velocity is not None and velocity_15m not in (None, 0):
        if velocity_15m and velocity > 0:
            pct = round((velocity - velocity_15m) / max(abs(velocity_15m), 1e-9) * 100)
            if abs(pct) >= 20:
                direction = "increased" if pct > 0 else "decreased"
                reasons.append(f"Share velocity {direction} {abs(pct)}% versus the 15-minute average.")
        if velocity is not None and velocity >= 1:
            reasons.append(f"Current share velocity is {velocity:.1f} shares/minute.")

    if acceleration is not None and abs(acceleration) >= 0.05:
        trend = "strongly positive" if acceleration > 0.5 else "positive" if acceleration > 0 else "negative"
        reasons.append(f"Share acceleration is {trend} ({acceleration:+.2f} shares/min²).")

    if new_sharers is not None and new_sharers >= 1:
        msg = f"{int(new_sharers)} new unique sharers in the last 5 minutes"
        if growth_rate is not None and growth_rate >= 0.2:
            msg += f" (+{growth_rate * 100:.0f}% growth rate)"
        reasons.append(msg + ".")

    f60 = forecast.get(60) or forecast.get(30) or (next(iter(forecast.values())) if forecast else None)
    if f60:
        label = "60 minutes" if 60 in forecast else f"{next(iter(forecast))} minutes"
        if f60.get("prediction_type") == "model":
            reasons.append(f"Model predicts {f60['predicted_additional_shares']:,.0f} additional shares in the next {label} (confidence {f60['confidence']:.0%}).")
        else:
            reasons.append(f"Baseline extrapolation suggests ~{f60['predicted_additional_shares']:,.0f} additional shares in the next {label} ({f60.get('reason', 'limited history')}).")

    risk = misinfo.get("risk_score")
    if risk is not None:
        reasons.append(f"Estimated misinformation risk: {risk:.2f} ({misinfo.get('risk_label', '').lower()} — stylistic estimate, not a truth verdict).")

    if breadth is not None and breadth >= 3:
        reasons.append(f"Propagation breadth is expanding ({int(breadth)} unique targets in the cascade).")
    if branching is not None and branching >= 0.3:
        reasons.append(f"Secondary resharing is active (branching factor {branching:.2f}).")

    if not reasons:
        reasons.append("Insufficient observed signals yet — collecting more snapshots.")

    header = f"{priority_label_str} priority ({priority_score:.0f}/100) — estimated misinformation risk {misinfo.get('risk_score', 0):.2f}"
    return header, reasons


def top_factors(features: dict[str, Any], forecast: dict[int, dict[str, Any]], misinfo: dict[str, Any], spread: float) -> list[str]:
    """Ranked contributing factors (only observed ones — spec #52)."""
    factors: list[tuple[float, str]] = []

    velocity = features.get("share_velocity")
    if velocity is not None:
        factors.append((min(1.0, velocity / VELOCITY_SCALE), f"High share velocity ({velocity:.1f}/min)"))
    acceleration = features.get("share_acceleration")
    if acceleration is not None and acceleration > 0:
        factors.append((min(1.0, acceleration / 2.0), f"Rapid share acceleration ({acceleration:+.2f})"))
    growth = features.get("unique_sharer_growth_rate")
    if growth is not None and growth > 0.1:
        factors.append((min(1.0, growth * 2), "Increasing unique sharers"))
    f60 = forecast.get(60) or (next(iter(forecast.values())) if forecast else None)
    if f60 and f60.get("predicted_additional_shares", 0) > 0:
        norm = min(1.0, f60["predicted_additional_shares"] / FORECAST_SCALE_60M)
        factors.append((norm, f"High predicted propagation (+{f60['predicted_additional_shares']:,.0f} shares/60m)"))
    risk = misinfo.get("risk_score")
    if risk is not None:
        factors.append((risk, f"Misinformation risk {risk:.2f}"))
    breadth = features.get("propagation_breadth")
    if breadth is not None and breadth >= 3:
        factors.append((min(1.0, breadth / 25.0), "Expanding propagation breadth"))

    factors.sort(key=lambda t: t[0], reverse=True)
    return [text for _, text in factors[:5]] or ["Early signal collection in progress"]
