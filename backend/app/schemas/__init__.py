"""Pydantic request/response schemas — every API contract is explicit."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ health
class HealthResponse(BaseModel):
    status: str = Field(description="healthy | degraded | unhealthy")
    database: str
    forecast_model: str
    ingestion: str
    platforms_active: list[str]
    version: str
    app_env: str
    time: datetime


# ------------------------------------------------------------------ platforms
class PlatformStatus(BaseModel):
    platform: str
    configured: bool
    status: str
    # Spec #28 canonical connector states: CONNECTED | DEGRADED | RATE_LIMITED |
    # AUTH_REQUIRED | UNAVAILABLE | DISABLED
    state: Optional[str] = None
    healthy: Optional[bool] = None
    detail: Optional[str] = None
    last_successful_fetch: Optional[datetime] = None
    last_error: Optional[str] = None
    request_count: Optional[int] = None
    error_count: Optional[int] = None
    rate_limit_status: Optional[str] = None


class PlatformListResponse(BaseModel):
    platforms: list[PlatformStatus]


# ------------------------------------------------------------------ posts
class MetricSnapshotOut(BaseModel):
    timestamp: datetime
    likes: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    views: Optional[int] = None
    followers: Optional[int] = None
    unique_sharers: Optional[int] = None


class PostOut(BaseModel):
    id: int
    platform: str
    external_post_id: str
    author_id: str
    author_display_name: Optional[str] = None
    content: str
    language: Optional[str] = None
    url: Optional[str] = None
    posted_at: datetime
    is_demo: bool
    latest_metrics: Optional[MetricSnapshotOut] = None


class PostListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    posts: list[PostOut]


class PaginatedMetrics(BaseModel):
    post_id: int
    total: int
    limit: int
    offset: int
    snapshots: list[MetricSnapshotOut]


# ------------------------------------------------------------------ features
class FeatureSnapshotOut(BaseModel):
    timestamp: datetime
    share_velocity: Optional[float] = None
    share_velocity_5m: Optional[float] = None
    share_velocity_15m: Optional[float] = None
    share_acceleration: Optional[float] = None
    engagement_velocity: Optional[float] = None
    engagement_acceleration: Optional[float] = None
    view_velocity: Optional[float] = None
    comment_velocity: Optional[float] = None
    like_velocity: Optional[float] = None
    unique_sharers: Optional[int] = None
    new_unique_sharers: Optional[float] = None
    unique_sharer_growth_rate: Optional[float] = None
    propagation_depth: Optional[float] = None
    propagation_breadth: Optional[float] = None
    cascade_size: Optional[int] = None
    branching_factor: Optional[float] = None
    avg_time_between_shares: Optional[float] = None
    median_time_between_shares: Optional[float] = None
    network_growth_rate: Optional[float] = None
    reshare_concentration: Optional[float] = None
    time_since_post: Optional[float] = None
    hour_of_day: Optional[int] = None
    day_of_week: Optional[int] = None
    is_weekend: Optional[bool] = None
    author_followers: Optional[int] = None
    engagement_ratio: Optional[float] = None
    shares_to_views_ratio: Optional[float] = None
    content_length: Optional[int] = None
    word_count: Optional[int] = None
    capital_ratio: Optional[float] = None
    hashtag_count: Optional[int] = None
    url_present: Optional[bool] = None
    sentiment_score: Optional[float] = None
    emotional_intensity: Optional[float] = None
    sensational_score: Optional[float] = None
    claim_score: Optional[float] = None
    urgency_score: Optional[float] = None

    model_config = {"extra": "allow"}  # pass through remaining feature fields


# ------------------------------------------------------------------ predictions
class ForecastPoint(BaseModel):
    horizon_minutes: int
    predicted_additional_shares: float
    predicted_total_shares: float
    confidence: float
    prediction_type: str
    reason: Optional[str] = None
    model: Optional[str] = None
    model_version: Optional[str] = None


class PredictionResponse(BaseModel):
    post_id: str
    platform: str
    current_shares: Optional[float] = None
    share_velocity: Optional[float] = None
    share_acceleration: Optional[float] = None
    engagement_velocity: Optional[float] = None
    unique_sharer_growth_rate: Optional[float] = None
    propagation_breadth: Optional[float] = None
    horizons: dict[str, ForecastPoint]
    spread_risk: float
    misinformation_risk: float
    misinformation_risk_label: str
    misinformation_model_layer: str
    intervention_priority: float
    priority_label: str
    explanation: list[str]
    explanation_header: str
    top_factors: list[str]


class PredictRequest(BaseModel):
    post_id: int = Field(description="Numeric PR•VISION post id")
    persist: bool = Field(default=True, description="Persist prediction to DB")


class ScoreAllResponse(BaseModel):
    scored: int
    results: list[PredictionResponse]


# ------------------------------------------------------------------ intervention
class InterventionOut(BaseModel):
    post_id: int
    timestamp: datetime
    spread_score: float
    misinformation_score: float
    intervention_priority: float
    priority_label: str
    explanation: Optional[str] = None
    top_factors: list[str] = []


# ------------------------------------------------------------------ dashboard
class DashboardSummary(BaseModel):
    posts_monitored: int
    critical_alerts: int
    high_risk_posts: int
    predicted_shares_60m: float
    average_risk: Optional[float] = None
    platform_counts: dict[str, int]
    label_counts: dict[str, int]
    ingestion: dict[str, Any]
    models: dict[str, Any]
    last_update: Optional[datetime] = None


class TrendingPost(BaseModel):
    post_id: int
    platform: str
    external_post_id: str
    content: str
    is_demo: bool
    current_shares: Optional[float]
    share_velocity: Optional[float]
    share_acceleration: Optional[float]
    intervention_priority: Optional[float]
    priority_label: Optional[str]
    misinformation_risk: Optional[float]
    predicted_additional_shares_60m: Optional[float]
    timestamp: datetime


class HighPriorityPost(BaseModel):
    post_id: int
    platform: str
    external_post_id: str
    content: str
    is_demo: bool
    current_shares: Optional[float]
    share_velocity: Optional[float]
    share_acceleration: Optional[float]
    predicted_additional_shares: Optional[float]
    misinformation_risk: Optional[float]
    intervention_priority: float
    priority_label: str
    top_factors: list[str]
    timestamp: datetime


class HighPriorityResponse(BaseModel):
    total: int
    posts: list[HighPriorityPost]


class TrendingResponse(BaseModel):
    total: int
    posts: list[TrendingPost]


# ------------------------------------------------------------------ demo
class DemoGenerateRequest(BaseModel):
    num_posts: int = Field(default=5, ge=1, le=20)
    archetypes: Optional[list[str]] = Field(
        default=None,
        description="Subset of: normal, trending, viral, suspicious_viral, false_alarm",
    )
    score: bool = Field(default=True, description="Run the full scoring pipeline after generation")


class DemoGenerateResponse(BaseModel):
    created: int
    posts: list[dict[str, Any]]


# ------------------------------------------------------------------ ingestion
class IngestionStartRequest(BaseModel):
    platforms: Optional[list[str]] = Field(default=None, description="Defaults to ['demo']")
    interval_seconds: Optional[int] = Field(default=None, ge=10, le=3600)


class IngestionStatusResponse(BaseModel):
    running: bool
    interval_seconds: int
    platforms: list[str]
    last_cycle_at: Optional[datetime] = None
    consecutive_failures: dict[str, int] = {}
    detail: str = ""


# ------------------------------------------------------------------ ml
class TrainResponse(BaseModel):
    status: str
    horizons: dict[str, Any] = {}
    misinformation: dict[str, Any] = {}
    detail: str = ""


class ModelStatusResponse(BaseModel):
    forecast: dict[str, Any]
    misinformation: dict[str, Any]
    feature_count: int
    runtime: dict[str, Any] | None = None  # numeric-runtime availability probe
    engine: str | None = None          # native | portable | baseline (what serves predictions)
    engine_note: str | None = None     # honest explanation when not native
