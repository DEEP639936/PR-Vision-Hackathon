"""Evaluation metrics for share forecasting (spec #21)."""
from __future__ import annotations

import math
from typing import Sequence


def mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    if not y_true:
        return float("nan")
    return sum(abs(t - p) for t, p in zip(y_true, y_pred)) / len(y_true)


def rmse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    if not y_true:
        return float("nan")
    return math.sqrt(sum((t - p) ** 2 for t, p in zip(y_true, y_pred)) / len(y_true))


def r2(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    n = len(y_true)
    if n < 2:
        return float("nan")
    mean = sum(y_true) / n
    ss_res = sum((t - p) ** 2 for t, p in zip(y_true, y_pred))
    ss_tot = sum((t - mean) ** 2 for t in y_true)
    if ss_tot == 0:
        return float("nan")
    return 1.0 - ss_res / ss_tot


def mape(y_true: Sequence[float], y_pred: Sequence[float], epsilon: float = 1e-6) -> float:
    """MAPE in percent, skipping near-zero truths (share deltas can be 0)."""
    ratios = [abs((t - p) / max(abs(t), epsilon)) for t, p in zip(y_true, y_pred) if abs(t) > epsilon]
    if not ratios:
        return float("nan")
    return 100.0 * sum(ratios) / len(ratios)


def evaluate(y_true: Sequence[float], y_pred: Sequence[float]) -> dict[str, float]:
    return {
        "mae": round(mae(y_true, y_pred), 3),
        "rmse": round(rmse(y_true, y_pred), 3),
        "r2": round(r2(y_true, y_pred), 4),
        "mape": round(mape(y_true, y_pred), 2),
    }
