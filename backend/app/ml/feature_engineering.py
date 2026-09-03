"""Feature engineering — computes all PR•VISION features from historical data.

STRICT CAUSALITY (spec #19 — no data leakage)
---------------------------------------------
Every function here receives only snapshots with `timestamp <= as_of`.
Features are pure functions of (metric history, propagation history, content),
which makes them unit-testable and safe for training/label alignment.

Feature families (spec #12-15):
    1. Share velocity      Δshares/Δt  over 1/5/15-minute windows
    2. Share acceleration  Δvelocity/Δt
    3. Engagement velocity / acceleration (likes+comments+shares, views, …)
    4. Unique sharer growth
    5. Propagation topology (depth, breadth, branching, cascade, inter-share
       timing, network growth, reshare concentration)
    6. Temporal context    (time since post, hour, weekend, …)
    7. Author signals      (followers, engagement ratios)
    8. NLP content signals (length, caps, punctuation, hashtags, sensational /
       claim-like / urgency lexicons, lightweight sentiment)
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------- lexicons
SENSATIONAL_TERMS = {
    "shocking", "unbelievable", "insane", "explosive", "bombshell", "exposed",
    "jaw-dropping", "mind-blowing", "you won't believe", "gone viral",
    "disturbing", "terrifying", "horrifying", "outrageous",
}
CLAIM_TERMS = {
    "cure", "miracle", "proven", "doctors hate", "secret", "banned",
    "they don't want", "conspiracy", "truth about", "leaked", "documents prove",
    "100%", "guaranteed", "instantly", "destroys", "detox", "immune",
    "chemicals", "toxic", "cover-up", "coverup", "whistleblower", "blackout",
}
URGENCY_TERMS = {
    "breaking", "urgent", "warning", "alert", "immediately", "now", "share",
    "forward", "before it's deleted", "before they", "hurry", "last chance",
    "act now", "spread the word", "share everywhere", "going fast",
}
POSITIVE_TERMS = {
    "good", "great", "love", "amazing", "wonderful", "beautiful", "happy",
    "thanks", "free", "win", "won", "proud", "best", "enjoy", "incredible",
}
NEGATIVE_TERMS = {
    "bad", "hate", "awful", "terrible", "worst", "scary", "angry", "sad",
    "crash", "death", "danger", "risk", "warning", "sick", "harm", "fear",
}
_EMOTIONAL_PUNCT = re.compile(r"!{2,}|\?{2,}|\*+|[\U0001F300-\U0001FAFF\u2755\u2757]")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+")
_WHITESPACE = re.compile(r"\s+")

EPS = 1e-9


# ================================================================ rate helpers
def per_minute(delta_value: float, delta_seconds: float) -> Optional[float]:
    """Safe rate: value change per minute (None if window is invalid)."""
    if delta_value is None or delta_seconds is None or delta_seconds <= 0:
        return None
    return float(delta_value) / (delta_seconds / 60.0)


def _latest_at_or_before(history: Sequence[dict], as_of: datetime) -> Optional[dict]:
    """Most recent snapshot with timestamp <= as_of (strictly causal lookup)."""
    chosen: Optional[dict] = None
    for snap in history:
        if snap["timestamp"] <= as_of:
            chosen = snap
        else:
            break
    return chosen


def _snapshot_before(history: Sequence[dict], as_of: datetime, min_age_seconds: float) -> Optional[dict]:
    """Most recent snapshot at least `min_age_seconds` older than as_of."""
    chosen: Optional[dict] = None
    for snap in history:
        age = (as_of - snap["timestamp"]).total_seconds()
        if age >= min_age_seconds:
            chosen = snap
        else:
            break
    return chosen


def _metric(snap: Optional[dict], key: str) -> Optional[float]:
    if snap is None:
        return None
    value = snap.get(key)
    return None if value is None else float(value)


# ============================================================== share dynamics
def share_velocity(history: Sequence[dict], as_of: datetime, window_seconds: float = 300.0) -> Optional[float]:
    """Δshares/Δt (shares per minute) over the trailing `window_seconds`."""
    current = _latest_at_or_before(history, as_of)
    past = _snapshot_before(history, as_of, window_seconds)
    if current is None or past is None or current is past:
        return None
    delta = (_metric(current, "shares") or 0.0) - (_metric(past, "shares") or 0.0)
    return per_minute(delta, (current["timestamp"] - past["timestamp"]).total_seconds())


def share_acceleration(history: Sequence[dict], as_of: datetime, window_seconds: float = 300.0) -> Optional[float]:
    """Δvelocity/Δt: change of share velocity between consecutive half-windows."""
    current = _latest_at_or_before(history, as_of)
    if current is None:
        return None
    recent = share_velocity(history, as_of, window_seconds)
    older = share_velocity(history, current["timestamp"] - timedelta_seconds(window_seconds), window_seconds)
    if recent is None or older is None:
        return None
    return (recent - older) / (window_seconds / 60.0)  # (shares/min) per minute


def timedelta_seconds(window_seconds: float) -> float:
    return timedelta(seconds=window_seconds)


def engagement_velocity(history: Sequence[dict], as_of: datetime, window_seconds: float = 300.0) -> Optional[float]:
    """Δ(likes + comments + shares)/Δt per minute."""
    current = _latest_at_or_before(history, as_of)
    past = _snapshot_before(history, as_of, window_seconds)
    if current is None or past is None or current is past:
        return None
    def total(snap: dict) -> Optional[float]:
        vals = [_metric(snap, k) for k in ("likes", "comments", "shares")]
        if all(v is None for v in vals):
            return None
        return sum(v for v in vals if v is not None)
    t_now, t_past = total(current), total(past)
    if t_now is None or t_past is None:
        return None
    return per_minute(t_now - t_past, (current["timestamp"] - past["timestamp"]).total_seconds())


def engagement_acceleration(history: Sequence[dict], as_of: datetime, window_seconds: float = 300.0) -> Optional[float]:
    current = _latest_at_or_before(history, as_of)
    if current is None:
        return None
    recent = engagement_velocity(history, as_of, window_seconds)
    older = engagement_velocity(history, current["timestamp"] - timedelta_seconds(window_seconds), window_seconds)
    if recent is None or older is None:
        return None
    return (recent - older) / (window_seconds / 60.0)


def single_metric_velocity(history: Sequence[dict], as_of: datetime, key: str, window_seconds: float = 300.0) -> Optional[float]:
    current = _latest_at_or_before(history, as_of)
    past = _snapshot_before(history, as_of, window_seconds)
    if current is None or past is None or current is past:
        return None
    delta = (_metric(current, key) or 0.0) - (_metric(past, key) or 0.0)
    return per_minute(delta, (current["timestamp"] - past["timestamp"]).total_seconds())


# ============================================================ unique sharers
def unique_sharer_features(history: Sequence[dict], as_of: datetime) -> dict[str, Optional[float]]:
    current = _latest_at_or_before(history, as_of)
    past = _snapshot_before(history, as_of, 300.0)
    uniq_now = _metric(current, "unique_sharers")
    uniq_past = _metric(past, "unique_sharers")
    new_unique = None
    growth_rate = None
    if uniq_now is not None and uniq_past is not None:
        new_unique = max(0.0, uniq_now - uniq_past)
        growth_rate = new_unique / (uniq_past + EPS) if uniq_past > 0 else (new_unique if new_unique > 0 else 0.0)
    return {"unique_sharers": uniq_now, "new_unique_sharers": new_unique, "unique_sharer_growth_rate": growth_rate}


# ============================================================= propagation
def propagation_features(events: Sequence[dict], as_of: datetime) -> dict[str, Optional[float]]:
    """Topology + timing features from reshare edges (empty when unavailable).

    branching_factor = secondary sharers (depth>=2) / primary sharers (depth 1)
    reshare_concentration = top-10% sharers' share of events (0-1), or None
    """
    valid = [dict(e, timestamp=_ensure_aware(e["timestamp"]))
             for e in events if _ensure_aware(e["timestamp"]) <= as_of]
    if not valid:
        return {
            "propagation_depth": None, "propagation_breadth": None,
            "cascade_size": None, "branching_factor": None,
            "avg_time_between_shares": None, "median_time_between_shares": None,
            "network_growth_rate": None, "reshare_concentration": None,
        }

    depths = [e["depth"] for e in valid if e.get("depth") is not None]
    times = sorted((e["timestamp"] for e in valid))
    gaps = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1) if times[i + 1] > times[i]]

    depth_max = max(depths) if depths else 1
    primary = sum(1 for d in depths if d == 1) if depths else len(valid)
    secondary = sum(1 for d in depths if d >= 2) if depths else 0
    breadth = len({e.get("target_user_id") for e in valid if e.get("target_user_id")})

    elapsed = max((as_of - times[0]).total_seconds(), 1.0) if times else 1.0
    network_growth = len(valid) / (elapsed / 60.0)  # events per minute

    counts: dict[str, int] = {}
    for e in valid:
        src = e.get("source_user_id")
        if src:
            counts[src] = counts.get(src, 0) + 1
    concentration = None
    if counts and len(counts) >= 5:
        ordered = sorted(counts.values(), reverse=True)
        top_k = max(1, len(ordered) // 10)
        concentration = sum(ordered[:top_k]) / len(valid)

    return {
        "propagation_depth": float(depth_max) if depths else None,
        "propagation_breadth": float(breadth),
        "cascade_size": len(valid),
        "branching_factor": (secondary / primary) if primary > 0 else None,
        "avg_time_between_shares": (sum(gaps) / len(gaps)) if gaps else None,
        "median_time_between_shares": _median(gaps) if gaps else None,
        "network_growth_rate": network_growth,
        "reshare_concentration": concentration,
    }


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# ================================================================ NLP features
def nlp_features(content: str) -> dict[str, Any]:
    """Lightweight, explainable content signals (spec #15)."""
    text = _WHITESPACE.sub(" ", (content or "")).strip()
    lower = text.lower()
    words = text.split()
    letters = [c for c in text if c.isalpha()]
    capital_ratio = (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0.0

    sensational = sum(1 for t in SENSATIONAL_TERMS if t in lower)
    claim = sum(1 for t in CLAIM_TERMS if t in lower)
    urgency = sum(1 for t in URGENCY_TERMS if t in lower)
    pos = sum(1 for t in POSITIVE_TERMS if re.search(rf"\b{re.escape(t)}\b", lower))
    neg = sum(1 for t in NEGATIVE_TERMS if re.search(rf"\b{re.escape(t)}\b", lower))

    sentiment = 0.0
    if pos + neg > 0:
        sentiment = (pos - neg) / (pos + neg)

    # intensity: exclamations + repeated punctuation + emoji density, normalised
    exclam = text.count("!")
    questions = text.count("?")
    emotive_punct = len(_EMOTIONAL_PUNCT.findall(text))
    intensity = min(1.0, (exclam * 0.12 + emotive_punct * 0.08 + capital_ratio * 1.5))

    return {
        "content_length": len(text),
        "word_count": len(words),
        "capital_ratio": round(capital_ratio, 4),
        "exclamation_count": exclam,
        "question_count": questions,
        "url_present": bool(_URL_RE.search(text)),
        "hashtag_count": len(_HASHTAG_RE.findall(text)),
        "sentiment_score": round(sentiment, 4),
        "emotional_intensity": round(intensity, 4),
        "sensational_score": round(min(1.0, sensational * 0.25 + capital_ratio * 2.0 + (exclam / 12.0)), 4),
        "claim_score": round(min(1.0, claim * 0.18), 4),
        "urgency_score": round(min(1.0, urgency * 0.15 + (1.0 if "!!!" in text else 0.0)), 4),
    }


# ============================================================== master builder
def build_feature_vector(
    *,
    post_posted_at: datetime,
    snapshot_history: Sequence[dict],
    propagation_events: Sequence[dict],
    content: str,
    author_followers: Optional[int] = None,
    as_of: Optional[datetime] = None,
    window_seconds: float = 300.0,
) -> dict[str, Any]:
    """Compute the full feature vector available at `as_of` (defaults: now).

    `snapshot_history` items: {timestamp, likes, comments, shares, views,
    followers, unique_sharers}. Anything later than `as_of` is ignored.
    """
    if as_of is None:
        if not snapshot_history:
            raise ValueError("as_of required when no snapshot history provided")
        as_of = snapshot_history[-1]["timestamp"]
    # normalise tz-awareness so naive/aware values compare safely
    as_of = _ensure_aware(as_of)
    history = [dict(s, timestamp=_ensure_aware(s["timestamp"]))
               for s in snapshot_history if _ensure_aware(s["timestamp"]) <= as_of]

    current = _latest_at_or_before(history, as_of)
    features: dict[str, Any] = {"timestamp": as_of}

    # --- velocities / accelerations
    features["share_velocity"] = _round(share_velocity(history, as_of, window_seconds))
    features["share_velocity_5m"] = _round(share_velocity(history, as_of, 300.0))
    features["share_velocity_15m"] = _round(share_velocity(history, as_of, 900.0))
    features["share_acceleration"] = _round(share_acceleration(history, as_of, window_seconds))
    features["engagement_velocity"] = _round(engagement_velocity(history, as_of, window_seconds))
    features["engagement_acceleration"] = _round(engagement_acceleration(history, as_of, window_seconds))
    features["view_velocity"] = _round(single_metric_velocity(history, as_of, "views", window_seconds))
    features["comment_velocity"] = _round(single_metric_velocity(history, as_of, "comments", window_seconds))
    features["like_velocity"] = _round(single_metric_velocity(history, as_of, "likes", window_seconds))

    # --- current raw metrics
    for key in ("likes", "comments", "shares", "views", "followers", "unique_sharers"):
        features[f"current_{key}"] = _metric(current, key)

    # --- unique sharers
    for key, value in unique_sharer_features(history, as_of).items():
        features[key] = _round(value)

    # --- propagation topology
    for key, value in propagation_features(propagation_events, as_of).items():
        features[key] = _round(value)

    # --- temporal
    posted_aware = _ensure_aware(post_posted_at)
    elapsed = (as_of - posted_aware).total_seconds()
    features["time_since_post"] = elapsed
    features["hour_of_day"] = as_of.hour
    features["minute_of_day"] = as_of.hour * 60 + as_of.minute
    features["day_of_week"] = as_of.weekday()
    features["is_weekend"] = as_of.weekday() >= 5

    # --- author + ratios
    followers = author_followers if author_followers is not None else features.get("current_followers")
    features["author_followers"] = followers
    eng_total = None
    vals = [features.get(f"current_{k}") for k in ("likes", "comments", "shares")]
    if all(v is not None for v in vals):
        eng_total = sum(vals)  # type: ignore[arg-type]
    if eng_total is not None and followers:
        features["engagement_ratio"] = _round(eng_total / (followers + EPS))
    else:
        features["engagement_ratio"] = None
    if features.get("current_shares") is not None and features.get("current_views"):
        features["shares_to_views_ratio"] = _round(features["current_shares"] / (features["current_views"] + EPS))
    else:
        features["shares_to_views_ratio"] = None

    # --- NLP
    features.update(nlp_features(content))
    return features


# numeric columns used as model inputs (defined once, shared with training)
MODEL_FEATURES = [
    "share_velocity", "share_velocity_5m", "share_velocity_15m", "share_acceleration",
    "engagement_velocity", "engagement_acceleration", "view_velocity", "comment_velocity", "like_velocity",
    "current_likes", "current_comments", "current_shares", "current_views", "current_followers", "current_unique_sharers",
    "new_unique_sharers", "unique_sharer_growth_rate",
    "propagation_depth", "propagation_breadth", "cascade_size", "branching_factor",
    "avg_time_between_shares", "median_time_between_shares", "network_growth_rate", "reshare_concentration",
    "time_since_post", "hour_of_day", "minute_of_day", "day_of_week", "is_weekend",
    "author_followers", "engagement_ratio", "shares_to_views_ratio",
    "content_length", "word_count", "capital_ratio", "exclamation_count", "question_count",
    "url_present", "hashtag_count", "sentiment_score", "emotional_intensity",
    "sensational_score", "claim_score", "urgency_score",
]


def _round(value: Any, digits: int = 6) -> Any:
    return round(value, digits) if isinstance(value, float) and math.isfinite(value) else value


def _ensure_aware(dt: datetime) -> datetime:
    """Normalise to UTC-aware so naive/aware datetimes can be compared."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
