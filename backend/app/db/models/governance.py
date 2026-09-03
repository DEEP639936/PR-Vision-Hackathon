"""PR•VISION governance domain — users, sessions, alerts, cases, audit, models.

Entities added per production spec:
    users              investigator/operator accounts
    api_tokens         DB-backed bearer sessions (hashed; revocable = logout)
    alerts             alert-engine output (spec #13: LOW/MEDIUM/HIGH/CRITICAL)
    cases              saved investigations (spec #14: save analysis as case)
    case_notes         investigator notes on a case
    audit_logs         who did what, when (spec #15, #17)
    model_versions     DB-backed model registry (spec #10/#15; JSON stays cache)

Design notes:
- Passwords are NEVER stored raw: pbkdf2_sha256 (see app.core.security).
- Tokens are stored as SHA-256 hashes — a DB leak does not leak sessions.
- Alerts reference either an early-warning post or a verification job (both
  nullable so the engine can raise platform-level alerts).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from . import Base, BigIntPK, TimestampMixin, utcnow


# ------------------------------------------------------------------------------- users
class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="analyst")  # admin|analyst|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tokens: Mapped[list["SessionToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    cases: Mapped[list["Case"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan")

    def to_dto(self) -> dict:
        return {"id": self.id, "email": self.email,
                "display_name": self.display_name, "role": self.role}


# -------------------------------------------------------------------------- api tokens
class SessionToken(Base):
    """Opaque bearer token; only the SHA-256 hash is persisted."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="tokens")

    __table_args__ = (Index("ix_api_tokens_user_active", "user_id", "expires_at"),)


# ------------------------------------------------------------------------------- alerts
class Alert(Base):
    """Alert-engine output. Severity: LOW|MEDIUM|HIGH|CRITICAL (spec #13)."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    # kind: misinfo_risk | acceleration_spike | forecast_jump | evidence_conflict | media_signal
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # supporting numbers
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=True, index=True)
    verification_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_alerts_created_severity", "created_at", "severity"),
        Index("ix_alerts_dedupe", "dedupe_key", "created_at"),
    )


# -------------------------------------------------------------------------------- cases
class Case(TimestampMixin, Base):
    """A saved investigation (spec #14): analysis snapshot + status + notes."""

    __tablename__ = "cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    verification_job_id: Mapped[int] = mapped_column(
        ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN", index=True)
    # OPEN | MONITORING | ESCALATED | CLOSED
    priority_snapshot: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verdict_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)

    owner: Mapped[User | None] = relationship(back_populates="cases")
    notes: Mapped[list["CaseNote"]] = relationship(
        back_populates="case", cascade="all, delete-orphan", order_by="CaseNote.created_at")

    __table_args__ = (
        Index("ix_cases_job_status", "verification_job_id", "status"),
        Index("ix_cases_created", "created_at"),
    )


class CaseNote(TimestampMixin, Base):
    __tablename__ = "case_notes"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_name: Mapped[str] = mapped_column(String(120), nullable=False, default="investigator")
    body: Mapped[str] = mapped_column(Text, nullable=False)

    case: Mapped[Case] = relationship(back_populates="notes")

    __table_args__ = (Index("ix_case_notes_case_created", "case_id", "created_at"),)


# --------------------------------------------------------------------------- audit logs
class AuditLog(Base):
    """Append-only trail of security-relevant actions (spec #17)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (Index("ix_audit_logs_created_action", "created_at", "action"),)


# ------------------------------------------------------------------------ model versions
class ModelVersion(Base):
    """DB-backed model registry row (mirrors ml/models/registry.json)."""

    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    task: Mapped[str] = mapped_column(String(32), nullable=False)  # forecast|misinformation
    horizon_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="registered")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_model_versions_name_version", "name", "version", unique=True),
    )
