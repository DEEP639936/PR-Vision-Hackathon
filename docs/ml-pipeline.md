# PR•VISION — ML Pipeline

## 1. Feature Engineering (strictly causal)

All features at time `t` use ONLY snapshots with `timestamp ≤ t` (spec #19).
The builder is a pure function, so the identical code path serves live scoring,
training backfill, and tests.

| Family | Features |
|--------|----------|
| Share dynamics | `share_velocity` (1/5/15-min windows, shares/min), `share_acceleration` (Δvelocity/min) |
| Engagement | `engagement_velocity`, `engagement_acceleration`, `view_velocity`, `comment_velocity`, `like_velocity` |
| Sharers | `unique_sharers`, `new_unique_sharers`, `unique_sharer_growth_rate` |
| Propagation | `propagation_depth`, `propagation_breadth`, `cascade_size`, `branching_factor` (= secondary/primary sharers), `avg/median_time_between_shares`, `network_growth_rate`, `reshare_concentration` |
| Temporal | `time_since_post`, `hour_of_day`, `minute_of_day`, `day_of_week`, `is_weekend` |
| Author | `author_followers`, `engagement_ratio`, `shares_to_views_ratio` |
| NLP | length, words, caps ratio, punctuation counts, URL/hashtag flags, lexicon scores (`sensational_score`, `claim_score`, `urgency_score`), lightweight sentiment, emotional intensity |

45 features total (`MODEL_FEATURES`). Missing platform metrics stay `None` and
are encoded as 0.0 for the model, with the raw NULL preserved in the DB.

## 2. Training Targets (spec #18)

For a feature snapshot at `t` and horizon `h ∈ {30, 60, 120}` minutes:

```
target_h = shares(t + h) − shares(t)
```

A future anchor must exist within ±3 minutes of `t + h`, otherwise the row is
skipped. Feature snapshots are joined against raw `metric_snapshots` (the
authoritative counter store) — never against denormalized copies.

## 3. Time-Aware Splitting (spec #20)

Rows are ordered chronologically and split **70 / 15 / 15** (train / validation
/ test) **without shuffling** — the test set always contains dynamics that
happened after the training window. XGBoost uses early stopping (30 rounds) on
the chronological validation slice.

## 4. Evaluation vs Baseline (spec #21)

Baseline: `current_share_velocity × horizon` (pure extrapolation). Reported per
horizon in `ml/models/registry.json`:

| Horizon | Rows | XGBoost MAE | Baseline MAE | XGBoost R² |
|---------|------|-------------|--------------|-----------|
| 30 min  | 562  | 80.7        | 190.8        | 0.869     |
| 60 min  | 420  | 180.2       | 400.3        | 0.828     |
| 120 min | 196  | 336.2       | 2060.3       | 0.758     |

*(measured on the seeded 10-post demo dataset; values scale with dataset)*

## 5. Model Versioning (spec #22)

Every training run appends to `ml/models/registry.json`:

```json
{
  "model_name": "prvision-share-forecast",
  "version": "2026.09.02-061443-26a2",
  "horizon_minutes": 60,
  "trained_at": "2026-09-02T06:14:43Z",
  "dataset_size": 420,
  "n_features": 45,
  "metrics": { "mae": 180.2, "rmse": 288.5, "r2": 0.828, "mape": …,
               "baseline_mae": 400.3, "improvement_vs_baseline_pct": 55.0 },
  "artifact": "ml/models/forecast_60m__<version>.joblib"
}
```

Predictions persist the `model_name` + `model_version` used. `ModelManager`
loads the latest artifacts once at startup (kept warm in memory) and hot-reloads
after `/api/ml/train`.

## 6. Cold-Start Fallback (spec #23)

`ModelManager.predict_additional_shares()` degrades transparently:

```json
{
  "prediction_type": "baseline",
  "reason": "insufficient historical data",
  "predicted_additional_shares": "velocity × horizon",
  "confidence": 0.30
}
```

Triggers: no trained artifact, or fewer than `PREDICTION_MIN_HISTORY_SNAPSHOTS`
(default 3) snapshots. Confidence for model predictions blends training R² with
a data-adequacy factor (full confidence at 12+ snapshots).

## 7. Misinformation-Risk Component (spec #16)

**Honest limitations first:** the supervised layer is trained on a *synthetic,
stylistically labeled* corpus (96 texts: misinfo-styled templates vs benign
templates). It estimates stylistic risk — NOT truth. The system never claims
"this post IS misinformation".

```
risk = 0.65 · P(TF-IDF 1-2gram → LogisticRegression)     [model layer]
     + 0.35 · lexicon heuristic                          [transparent layer]
```

The heuristic (claim/sensational/urgency lexicons + punctuation + caps) is
documented, explainable, and alone sufficient when no artifact exists
(`layer = "heuristic"` in every response). Labels:
0.00–0.30 LOW · 0.30–0.60 MODERATE · 0.60–0.80 HIGH · 0.80–1.00 CRITICAL.

## 8. Intervention Priority (spec #24)

```
spread_risk      = soft-OR( logistic(forecast60), logistic(velocity), logistic(sharer growth) )
misinformation   = risk from §7
priority(0-100)  = (w_spread·spread + w_misinfo·risk) × 100     [weights configurable]
```

Defaults `w_spread=0.60`, `w_misinfo=0.40` (env-tunable). Squashing scales are
named constants in `scoring_service.py` (1000 shares/60m, 15 shares/min).
Explanations are generated ONLY from observed features; top factors are ranked
by their normalized contribution.
