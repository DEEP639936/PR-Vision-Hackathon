"""Repositories for the governance domain (users, tokens, alerts, cases, audit).

Same conventions as the core repositories: static methods, paginated where
hot, defensive parsing, no business logic.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Alert, AuditLog, Case, CaseNote, ModelVersion, SessionToken, User


def hash_token(token: str) -> str:
    """SHA-256 of the bearer token (kept local to avoid a circular import
    with app.core.security, which depends on repositories)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------- users
class UserRepository:
    @staticmethod
    def get_by_email(db: Session, email: str) -> User | None:
        stmt = select(User).where(func.lower(User.email) == (email or "").strip().lower())
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def get_by_id(db: Session, user_id: int) -> User | None:
        return db.get(User, user_id)

    @staticmethod
    def create(db: Session, *, email: str, password_hash: str, display_name: str,
               role: str = "analyst") -> User:
        user = User(email=email.strip().lower(), password_hash=password_hash,
                    display_name=display_name.strip() or email.split("@")[0], role=role)
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def count(db: Session) -> int:
        return int(db.execute(select(func.count(User.id))).scalar_one())

    @staticmethod
    def touch_login(db: Session, user: User) -> None:
        user.last_login_at = datetime.now(timezone.utc)
        db.flush()


# ------------------------------------------------------------------------------ tokens
class SessionTokenRepository:
    @staticmethod
    def issue(db: Session, *, user_id: int, token: str, expires_hours: int,
              user_agent: str | None = None, ip: str | None = None) -> SessionToken:
        row = SessionToken(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=expires_hours),
            user_agent=(user_agent or "")[:255] or None,
            ip=(ip or "")[:64] or None,
        )
        db.add(row)
        db.flush()
        return row

    @staticmethod
    def resolve(db: Session, token: str) -> tuple[User, SessionToken] | None:
        """Return (user, token_row) when the token is valid & unexpired."""
        row = db.execute(
            select(SessionToken).where(SessionToken.token_hash == hash_token(token))
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return None
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None
        user = db.get(User, row.user_id)
        if user is None or not user.is_active:
            return None
        return user, row

    @staticmethod
    def revoke(db: Session, token_hash: str) -> bool:
        row = db.execute(
            select(SessionToken).where(SessionToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = datetime.now(timezone.utc)
        db.flush()
        return True

    @staticmethod
    def revoke_all_for_user(db: Session, user_id: int) -> int:
        rows = db.execute(select(SessionToken).where(
            SessionToken.user_id == user_id, SessionToken.revoked_at.is_(None))).scalars().all()
        now = datetime.now(timezone.utc)
        for r in rows:
            r.revoked_at = now
        db.flush()
        return len(rows)

    @staticmethod
    def hash_of(token: str) -> str:
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------- alerts
class AlertRepository:
    @staticmethod
    def add(db: Session, **fields) -> Alert:
        alert = Alert(**fields)
        db.add(alert)
        db.flush()
        return alert

    @staticmethod
    def recent_duplicate(db: Session, dedupe_key: str, within_minutes: int = 45) -> Alert | None:
        """Suppress repeat alerts for the same condition inside the window."""
        if not dedupe_key:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
        stmt = (select(Alert).where(Alert.dedupe_key == dedupe_key, Alert.created_at >= cutoff)
                .order_by(Alert.created_at.desc()).limit(1))
        return db.execute(stmt).scalar_one_or_none()

    @staticmethod
    def list_alerts(
        db: Session,
        *,
        severity: str | None = None,
        acknowledged: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Alert], int]:
        stmt = select(Alert)
        count_stmt = select(func.count(Alert.id))
        if severity:
            stmt = stmt.where(Alert.severity == severity.upper())
            count_stmt = count_stmt.where(Alert.severity == severity.upper())
        if acknowledged is True:
            stmt = stmt.where(Alert.acknowledged_at.is_not(None))
            count_stmt = count_stmt.where(Alert.acknowledged_at.is_not(None))
        if acknowledged is False:
            stmt = stmt.where(Alert.acknowledged_at.is_(None))
            count_stmt = count_stmt.where(Alert.acknowledged_at.is_(None))
        total = db.execute(count_stmt).scalar_one()
        rows = db.execute(stmt.order_by(Alert.created_at.desc()).limit(limit).offset(offset)).scalars().all()
        return rows, int(total)

    @staticmethod
    def acknowledge(db: Session, alert_id: int, by: str) -> Alert | None:
        alert = db.get(Alert, alert_id)
        if alert is None:
            return None
        if alert.acknowledged_at is None:
            alert.acknowledged_at = datetime.now(timezone.utc)
            alert.acknowledged_by = (by or "investigator")[:255]
            db.flush()
        return alert

    @staticmethod
    def counts_by_severity(db: Session) -> dict[str, int]:
        stmt = (select(Alert.severity, func.count(Alert.id))
                .where(Alert.acknowledged_at.is_(None))
                .group_by(Alert.severity))
        return {sev: int(cnt) for sev, cnt in db.execute(stmt).all()}


# -------------------------------------------------------------------------------- cases
class CaseRepository:
    @staticmethod
    def create(db: Session, **fields) -> Case:
        case = Case(**fields)
        db.add(case)
        db.flush()
        return case

    @staticmethod
    def get(db: Session, case_id: int) -> Case | None:
        return db.get(Case, case_id)

    @staticmethod
    def for_job(db: Session, job_id: int) -> Sequence[Case]:
        return db.execute(
            select(Case).where(Case.verification_job_id == job_id)
            .order_by(Case.created_at.desc())).scalars().all()

    @staticmethod
    def list_cases(
        db: Session,
        *,
        status: str | None = None,
        created_by: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Case], int]:
        stmt = select(Case)
        count_stmt = select(func.count(Case.id))
        if status:
            stmt = stmt.where(Case.status == status.upper())
            count_stmt = count_stmt.where(Case.status == status.upper())
        if created_by is not None:
            stmt = stmt.where(Case.created_by == created_by)
            count_stmt = count_stmt.where(Case.created_by == created_by)
        total = db.execute(count_stmt).scalar_one()
        rows = db.execute(stmt.order_by(Case.created_at.desc()).limit(limit).offset(offset)).scalars().all()
        return rows, int(total)

    @staticmethod
    def add_note(db: Session, **fields) -> CaseNote:
        note = CaseNote(**fields)
        db.add(note)
        db.flush()
        return note

    @staticmethod
    def notes_for(db: Session, case_id: int) -> Sequence[CaseNote]:
        return db.execute(
            select(CaseNote).where(CaseNote.case_id == case_id)
            .order_by(CaseNote.created_at.asc())).scalars().all()

    @staticmethod
    def delete(db: Session, case: Case) -> None:
        db.delete(case)
        db.flush()


# --------------------------------------------------------------------------- audit logs
class AuditRepository:
    @staticmethod
    def record(db: Session, *, actor: str, action: str, target_type: str | None = None,
               target_id: str | int | None = None, detail: str | None = None,
               ip: str | None = None) -> AuditLog:
        entry = AuditLog(
            actor=(actor or "system")[:255],
            action=(action or "unknown")[:64],
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=(detail or "")[:2000] or None,
            ip=(ip or "")[:64] or None,
        )
        db.add(entry)
        db.flush()
        return entry

    @staticmethod
    def recent(db: Session, limit: int = 50) -> Sequence[AuditLog]:
        return db.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).scalars().all()


# ------------------------------------------------------------------------ model versions
class ModelVersionRepository:
    @staticmethod
    def register(db: Session, *, name: str, version: str, task: str,
                 horizon_minutes: int | None = None, artifact_path: str | None = None,
                 metrics: dict | None = None, status: str = "registered") -> ModelVersion:
        row = db.execute(select(ModelVersion).where(
            ModelVersion.name == name, ModelVersion.version == version)).scalar_one_or_none()
        if row is None:
            row = ModelVersion(name=name, version=version, task=task)
            db.add(row)
        row.task = task
        row.horizon_minutes = horizon_minutes
        row.artifact_path = artifact_path
        row.metrics = metrics or {}
        row.status = status
        db.flush()
        return row

    @staticmethod
    def latest_by_name(db: Session, name: str) -> ModelVersion | None:
        return db.execute(
            select(ModelVersion).where(ModelVersion.name == name)
            .order_by(ModelVersion.registered_at.desc()).limit(1)).scalar_one_or_none()

    @staticmethod
    def all_versions(db: Session, limit: int = 100) -> Sequence[ModelVersion]:
        return db.execute(
            select(ModelVersion).order_by(ModelVersion.registered_at.desc()).limit(limit)).scalars().all()


def alert_metrics_json(metrics: dict | None) -> str:
    return json.dumps(metrics or {}, ensure_ascii=False, default=str)
