"""PR•VISION ORM models.

Entities (per specification):
    posts, metric_snapshots, propagation_events, feature_snapshots,
    predictions, misinformation_scores, intervention_scores,
    data_source_status

Design notes:
- All temporal columns are timezone-aware UTC datetimes (DateTime(timezone=True)).
  On MySQL these map to DATETIME(6) with explicit UTC conversion at the
  application boundary (dates are always stored in UTC).
- Foreign keys + indexes on every hot query path (post_id, timestamp).
- `platform` uses an enumerated string (validated at schema level) so new
  platforms can be added without a migration.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone-aware UTC now (single source of truth for timestamps)."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# BigInteger PKs must alias to INTEGER on SQLite for autoincrement to work.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Platform(str, __import__("enum").Enum):
    DEMO = "demo"
    X = "x"
    REDDIT = "reddit"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    LINKEDIN = "linkedin"


# --------------------------------------------------------------------------- posts
class Post(TimestampMixin, Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    author_id: Mapped[str] = mapped_column(String(255), nullable=False)
    author_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    metric_snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="MetricSnapshot.timestamp"
    )
    propagation_events: Mapped[list["PropagationEvent"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    feature_snapshots: Mapped[list["FeatureSnapshot"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="FeatureSnapshot.timestamp"
    )
    predictions: Mapped[list["Prediction"]] = relationship(back_populates="post", cascade="all, delete-orphan")
    misinformation_scores: Mapped[list["MisinformationScore"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
    intervention_scores: Mapped[list["InterventionScore"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", order_by="InterventionScore.timestamp"
    )

    __table_args__ = (
        UniqueConstraint("platform", "external_post_id", name="uq_posts_platform_external_id"),
        Index("ix_posts_platform_posted_at", "platform", "posted_at"),
    )


# ---------------------------------------------------------------- metric snapshots
class MetricSnapshot(Base):
    """Raw platform metrics captured over time — the atom of all analysis."""

    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    followers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unique_sharers: Mapped[int | None] = mapped_column(Integer, nullable=True)

    post: Mapped[Post] = relationship(back_populates="metric_snapshots")

    __table_args__ = (
        UniqueConstraint("post_id", "timestamp", name="uq_metric_snapshot_post_ts"),
        Index("ix_metric_snapshots_post_ts", "post_id", "timestamp"),
    )


# -------------------------------------------------------------- propagation events
class PropagationEvent(Base):
    """A single reshare/repost edge in the propagation cascade (where exposed)."""

    __tablename__ = "propagation_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    source_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, default="share")  # share|repost|quote
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_since_original_post: Mapped[float | None] = mapped_column(Float, nullable=True)  # seconds
    depth: Mapped[int | None] = mapped_column(Integer, nullable=True)  # hops from origin (0 = origin)

    post: Mapped[Post] = relationship(back_populates="propagation_events")

    __table_args__ = (
        Index("ix_propagation_events_post_ts", "post_id", "timestamp"),
        Index("ix_propagation_events_post_depth", "post_id", "depth"),
    )


# --------------------------------------------------------------- feature snapshots
class FeatureSnapshot(Base):
    """Engineered temporal/propagation features at time t (strictly causal)."""

    __tablename__ = "feature_snapshots"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    # velocity / acceleration
    share_velocity: Mapped[float | None] = mapped_column(Float)              # shares per minute (recent window)
    share_velocity_5m: Mapped[float | None] = mapped_column(Float)
    share_velocity_15m: Mapped[float | None] = mapped_column(Float)
    share_acceleration: Mapped[float | None] = mapped_column(Float)
    engagement_velocity: Mapped[float | None] = mapped_column(Float)
    engagement_acceleration: Mapped[float | None] = mapped_column(Float)
    view_velocity: Mapped[float | None] = mapped_column(Float)
    comment_velocity: Mapped[float | None] = mapped_column(Float)
    like_velocity: Mapped[float | None] = mapped_column(Float)

    # sharers / propagation
    unique_sharers: Mapped[int | None] = mapped_column(Integer)
    new_unique_sharers: Mapped[float | None] = mapped_column(Float)
    unique_sharer_growth_rate: Mapped[float | None] = mapped_column(Float)
    propagation_depth: Mapped[float | None] = mapped_column(Float)
    propagation_breadth: Mapped[float | None] = mapped_column(Float)
    cascade_size: Mapped[int | None] = mapped_column(Integer)
    branching_factor: Mapped[float | None] = mapped_column(Float)
    avg_time_between_shares: Mapped[float | None] = mapped_column(Float)
    median_time_between_shares: Mapped[float | None] = mapped_column(Float)
    network_growth_rate: Mapped[float | None] = mapped_column(Float)
    reshare_concentration: Mapped[float | None] = mapped_column(Float)

    # temporal context
    time_since_post: Mapped[float | None] = mapped_column(Float)             # seconds
    hour_of_day: Mapped[int | None] = mapped_column(Integer)
    minute_of_day: Mapped[int | None] = mapped_column(Integer)
    day_of_week: Mapped[int | None] = mapped_column(Integer)
    is_weekend: Mapped[bool | None] = mapped_column(Boolean)

    # author + content
    author_followers: Mapped[int | None] = mapped_column(Integer)
    engagement_ratio: Mapped[float | None] = mapped_column(Float)            # engagement per view (or per follower)
    shares_to_views_ratio: Mapped[float | None] = mapped_column(Float)

    # NLP content features
    content_length: Mapped[int | None] = mapped_column(Integer)
    word_count: Mapped[int | None] = mapped_column(Integer)
    capital_ratio: Mapped[float | None] = mapped_column(Float)
    exclamation_count: Mapped[int | None] = mapped_column(Integer)
    question_count: Mapped[int | None] = mapped_column(Integer)
    url_present: Mapped[bool | None] = mapped_column(Boolean)
    hashtag_count: Mapped[int | None] = mapped_column(Integer)
    sentiment_score: Mapped[float | None] = mapped_column(Float)
    emotional_intensity: Mapped[float | None] = mapped_column(Float)
    sensational_score: Mapped[float | None] = mapped_column(Float)
    claim_score: Mapped[float | None] = mapped_column(Float)
    urgency_score: Mapped[float | None] = mapped_column(Float)

    post: Mapped[Post] = relationship(back_populates="feature_snapshots")

    __table_args__ = (
        UniqueConstraint("post_id", "timestamp", name="uq_feature_snapshot_post_ts"),
        Index("ix_feature_snapshots_post_ts", "post_id", "timestamp"),
    )


# --------------------------------------------------------------------- predictions
class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    prediction_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    horizon_minutes: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    predicted_additional_shares: Mapped[float] = mapped_column(Float, nullable=False)
    predicted_total_shares: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    prediction_type: Mapped[str] = mapped_column(String(16), nullable=False, default="model")  # model|baseline
    model_name: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(32))

    post: Mapped[Post] = relationship(back_populates="predictions")

    __table_args__ = (
        Index("ix_predictions_post_ts_horizon", "post_id", "prediction_timestamp", "horizon_minutes"),
    )


# -------------------------------------------------------------- misinformation scores
class MisinformationScore(Base):
    __tablename__ = "misinformation_scores"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False)  # 0.0 - 1.0
    risk_label: Mapped[str] = mapped_column(String(16), nullable=False)  # LOW|MODERATE|HIGH|CRITICAL
    model_version: Mapped[str | None] = mapped_column(String(32))

    post: Mapped[Post] = relationship(back_populates="misinformation_scores")

    __table_args__ = (Index("ix_misinfo_post_ts", "post_id", "timestamp"),)


# -------------------------------------------------------------- intervention scores
class InterventionScore(Base):
    __tablename__ = "intervention_scores"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    spread_score: Mapped[float] = mapped_column(Float, nullable=False)            # 0-1
    misinformation_score: Mapped[float] = mapped_column(Float, nullable=False)    # 0-1
    intervention_priority: Mapped[float] = mapped_column(Float, nullable=False)   # 0-100
    priority_label: Mapped[str] = mapped_column(String(16), nullable=False)       # LOW|MEDIUM|HIGH|CRITICAL
    explanation: Mapped[str | None] = mapped_column(Text)
    top_factors: Mapped[str | None] = mapped_column(Text)  # JSON array string
    model_version: Mapped[str | None] = mapped_column(String(32))

    post: Mapped[Post] = relationship(back_populates="intervention_scores")

    __table_args__ = (
        Index("ix_intervention_post_ts", "post_id", "timestamp"),
        Index("ix_intervention_priority", "intervention_priority"),
    )


# ----------------------------------------------------------------- data source status
class DataSourceStatus(Base):
    __tablename__ = "data_source_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_configured")
    # configured | healthy | degraded | error | not_configured | disabled
    last_successful_fetch: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate_limit_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# Import the multimodal verification domain so Base.metadata sees all tables.
# (Imported last: uses the shared Base/BigIntPK/TimestampMixin defined above.)
from .verification import (  # noqa: E402,F401
    AnalyzedContent,
    Claim,
    ClaimVerdict,
    EvidenceEdge,
    EvidenceItem,
    FactCheckMatch,
    MediaAnalysis,
    NumericalCheck,
    SourceProfile,
    TimelineEvent,
    VerificationJob,
)

# Governance domain: users/sessions/alerts/cases/audit/model registry.
from .governance import (  # noqa: E402,F401
    Alert,
    AuditLog,
    Case,
    CaseNote,
    ModelVersion,
    SessionToken,
    User,
)

__all__ = [
    "Base", "BigIntPK", "TimestampMixin", "Platform",
    "Post", "MetricSnapshot", "PropagationEvent", "FeatureSnapshot",
    "Prediction", "MisinformationScore", "InterventionScore", "DataSourceStatus",
    # verification domain
    "VerificationJob", "AnalyzedContent", "Claim", "ClaimVerdict",
    "EvidenceItem", "FactCheckMatch", "SourceProfile", "MediaAnalysis",
    "NumericalCheck", "TimelineEvent", "EvidenceEdge",
    # governance domain
    "User", "SessionToken", "Alert", "Case", "CaseNote", "AuditLog", "ModelVersion",
]
