"""PR•VISION — multimodal verification domain models.

Entities:
    verification_jobs   one user-submitted verification request (URL/text/file)
    analyzed_contents   normalized content + provenance extracted from the input
    claims              extracted factual claims (typed, with entities)
    evidence_items      retrieved external/web evidence (with citations)
    fact_check_matches  existing professional fact-checks found for claims
    source_profiles     transparent source-quality profiles (per host)
    evidence_edges      evidence-graph edges (claim↔evidence↔source↔entity)
    media_analyses      image/PDF/document forensic analyses
    numerical_checks    deterministic arithmetic / table consistency checks
    verdict_records     fused verdict + confidence + intervention priority
    timeline_events     evidence timeline (per job)

All conclusions are stored WITH their evidence so every element of the report
is traceable (spec #37). Nothing here asserts ground truth — the platform
produces evidence-based assessments for human investigators.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import Base, BigIntPK, TimestampMixin, utcnow


# ------------------------------------------------------------------ verification jobs
class VerificationJob(TimestampMixin, Base):
    __tablename__ = "verification_jobs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    # queued | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    input_kind: Mapped[str] = mapped_column(String(32), nullable=False)   # url|text|image|pdf|docx|screenshot|csv|html|json
    input_label: Mapped[str | None] = mapped_column(String(512))          # filename / URL / text preview
    submitted_by: Mapped[str | None] = mapped_column(String(128))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0-100
    stage: Mapped[str | None] = mapped_column(String(64))                 # human-readable current stage
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # compact JSON summary of the final report header (overall verdict, risk,
    # priority, provider states) — the full report is assembled from the
    # per-artifact tables, this accelerates the queue/list views.
    result_summary: Mapped[str | None] = mapped_column(Text)

    content: Mapped["AnalyzedContent | None"] = relationship(back_populates="job", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (Index("ix_vjobs_status_created", "status", "created_at"),)


# ----------------------------------------------------------------- analyzed content
class AnalyzedContent(TimestampMixin, Base):
    __tablename__ = "analyzed_contents"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)

    content_type: Mapped[str] = mapped_column(String(32), nullable=False)  # article|social_post|image|pdf|docx|text|csv|html|json|screenshot
    language: Mapped[str | None] = mapped_column(String(16))

    title: Mapped[str | None] = mapped_column(String(512))
    author: Mapped[str | None] = mapped_column(String(512))
    publisher: Mapped[str | None] = mapped_column(String(512))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    original_url: Mapped[str | None] = mapped_column(String(2048))
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    redirect_chain: Mapped[str | None] = mapped_column(Text)        # JSON array string
    og_metadata: Mapped[str | None] = mapped_column(Text)           # JSON object string
    fetch_status: Mapped[str | None] = mapped_column(String(32))    # ok|robots_blocked|auth_required|paywall|error|skipped
    http_status: Mapped[int | None] = mapped_column(Integer)

    raw_text: Mapped[str | None] = mapped_column(Text)              # extracted main text
    text_stats: Mapped[str | None] = mapped_column(Text)            # JSON {words, sentences, reading_time_min...}
    file_meta: Mapped[str | None] = mapped_column(Text)             # JSON {size, sha256, mime, pages...}

    # data classification per spec #29
    source_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="LIVE")  # LIVE|SIMULATED|DERIVED|MODEL_PREDICTION|EXTERNAL_EVIDENCE

    job: Mapped[VerificationJob] = relationship(back_populates="content")
    claims: Mapped[list["Claim"]] = relationship(back_populates="content", cascade="all, delete-orphan")
    media_analyses: Mapped[list["MediaAnalysis"]] = relationship(back_populates="content", cascade="all, delete-orphan")


# --------------------------------------------------------------------------- claims
class Claim(TimestampMixin, Base):
    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("analyzed_contents.id", ondelete="CASCADE"), nullable=False, index=True)

    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)          # CLAIM 01, 02...
    text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(24), nullable=False)    # FACTUAL|OPINION|PREDICTION|QUESTION|SATIRE|EMOTIONAL
    checkable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    claim_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    time_context: Mapped[str | None] = mapped_column(String(255))          # "yesterday", "2025-08-01", ...
    entities: Mapped[str | None] = mapped_column(Text)                     # JSON array of {name, type}
    numbers: Mapped[str | None] = mapped_column(Text)                      # JSON array of numeric facts found

    extraction_method: Mapped[str] = mapped_column(String(24), nullable=False, default="heuristic")  # heuristic|llm|hybrid

    content: Mapped[AnalyzedContent] = relationship(back_populates="claims")
    verdict: Mapped["ClaimVerdict | None"] = relationship(back_populates="claim", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("job_id", "ordinal", name="uq_claims_job_ordinal"),
        Index("ix_claims_job", "job_id", "ordinal"),
    )


# ---------------------------------------------------------- per-claim fused verdict
class ClaimVerdict(TimestampMixin, Base):
    __tablename__ = "claim_verdicts"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    verdict: Mapped[str] = mapped_column(String(32), nullable=False)   # spec #35 taxonomy
    confidence: Mapped[float] = mapped_column(Float, nullable=False)   # 0-1
    confidence_rationale: Mapped[str | None] = mapped_column(Text)

    supporting_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contradicting_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    neutral_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    primary_source_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    temporal_flag: Mapped[str | None] = mapped_column(String(32))      # CURRENT|OUTDATED|ANACHRONISTIC|UNSURE
    explanation: Mapped[str | None] = mapped_column(Text)              # human-readable reasoning chain
    fused_signals: Mapped[str | None] = mapped_column(Text)            # JSON of individual signal contributions

    claim: Mapped[Claim] = relationship(back_populates="verdict")


# ------------------------------------------------------------------- evidence items
class EvidenceItem(TimestampMixin, Base):
    __tablename__ = "evidence_items"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), index=True)

    provider: Mapped[str] = mapped_column(String(48), nullable=False)        # zai_web_search|google_factcheck|wikipedia|...
    url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str | None] = mapped_column(String(512))
    snippet: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[str | None] = mapped_column(String(64))             # raw date string from provider
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    stance: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")  # supports|contradicts|neutral
    stance_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    relevance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)        # 0-1
    quality: Mapped[float | None] = mapped_column(Float)                     # source-quality 0-1 (ranking layer)

    source_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="EXTERNAL_EVIDENCE")
    independence_cluster: Mapped[str | None] = mapped_column(String(64))     # cluster id for syndication detection (#41)

    __table_args__ = (Index("ix_evidence_job_claim", "job_id", "claim_id"),)


# -------------------------------------------------------------- fact-check matches
class FactCheckMatch(TimestampMixin, Base):
    __tablename__ = "fact_check_matches"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), index=True)

    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    claim_text: Mapped[str | None] = mapped_column(Text)
    textual_rating: Mapped[str | None] = mapped_column(String(128))          # False / Misleading / True / ...
    publisher: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[str | None] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(String(2048))
    review_snippet: Mapped[str | None] = mapped_column(Text)
    similarity: Mapped[float | None] = mapped_column(Float)                  # 0-1 claim similarity

    source_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="EXTERNAL_EVIDENCE")


# ------------------------------------------------------------------ source profiles
class SourceProfile(TimestampMixin, Base):
    __tablename__ = "source_profiles"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    display_name: Mapped[str | None] = mapped_column(String(255))
    quality: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)          # 0-1 contextual score
    signals: Mapped[str | None] = mapped_column(Text)                                    # JSON array of {signal, effect, note}
    classification: Mapped[str | None] = mapped_column(String(64))                       # official|academic|news|fact_checker|social|blog|unknown|...

    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------- media analysis
class MediaAnalysis(TimestampMixin, Base):
    __tablename__ = "media_analyses"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    content_id: Mapped[int | None] = mapped_column(ForeignKey("analyzed_contents.id", ondelete="CASCADE"), index=True)

    media_type: Mapped[str] = mapped_column(String(24), nullable=False)      # image|pdf|docx|csv|html|json|screenshot
    file_name: Mapped[str | None] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)

    analysis: Mapped[str | None] = mapped_column(Text)                       # JSON — engine-specific full result
    ocr_text: Mapped[str | None] = mapped_column(Text)                       # extracted text (images/PDFs)
    manipulation_risk: Mapped[float | None] = mapped_column(Float)           # 0-1 heuristic signals
    ai_generation_signal: Mapped[float | None] = mapped_column(Float)        # 0-1, model-based only if detector available
    ai_signal_confidence: Mapped[str | None] = mapped_column(String(16))     # LOW|MEDIUM|HIGH
    authenticity_note: Mapped[str | None] = mapped_column(Text)              # honest limitation statement

    detectors_run: Mapped[str | None] = mapped_column(Text)                  # JSON array of detector names+versions
    source_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="DERIVED")

    content: Mapped[AnalyzedContent] = relationship(back_populates="media_analyses")


# ------------------------------------------------------------------ numerical checks
class NumericalCheck(TimestampMixin, Base):
    __tablename__ = "numerical_checks"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), index=True)

    check_type: Mapped[str] = mapped_column(String(48), nullable=False)      # percentage_bound|total_sum|unit_consistency|date_arithmetic|table_growth|chart_scale
    subject: Mapped[str | None] = mapped_column(String(512))                 # what was checked
    expected: Mapped[str | None] = mapped_column(String(128))
    observed: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), nullable=False)          # consistent|inconsistent|unverifiable
    detail: Mapped[str | None] = mapped_column(Text)                         # deterministic explanation incl. arithmetic

    source_classification: Mapped[str] = mapped_column(String(32), nullable=False, default="DERIVED")


# --------------------------------------------------------------------- timeline
class TimelineEvent(TimestampMixin, Base):
    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)

    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # real-world event time if known
    occurred_at_raw: Mapped[str | None] = mapped_column(String(128))               # provider date string when unparseable
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="evidence")  # publication|claim|fact_check|propagation|detection|evidence
    url: Mapped[str | None] = mapped_column(String(2048))

    __table_args__ = (Index("ix_timeline_job_time", "job_id", "occurred_at"),)


# ------------------------------------------------------------------- evidence graph
class EvidenceEdge(TimestampMixin, Base):
    __tablename__ = "evidence_edges"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)

    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)   # claim|article|source|fact_check|person|organization|event|image|document|url
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)   # stable node key within the job
    edge_type: Mapped[str] = mapped_column(String(24), nullable=False)     # supports|contradicts|references|published_by|mentions|derived_from|related_to
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_key: Mapped[str] = mapped_column(String(255), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    note: Mapped[str | None] = mapped_column(String(512))

    __table_args__ = (
        UniqueConstraint("job_id", "source_key", "edge_type", "target_key", name="uq_edge"),
        Index("ix_edges_job", "job_id", "source_key"),
    )
