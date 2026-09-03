"""Authentication API (spec #3, #17).

Endpoints:
    POST /api/auth/register  create account → bearer token
    POST /api/auth/login     email+password → bearer token
    POST /api/auth/logout    revoke the presented token
    GET  /api/auth/me        current user profile

Passwords: PBKDF2-HMAC-SHA256 (see app.core.security). Sessions: random
opaque tokens, only SHA-256 hashes stored, revocable. Registration,
login and logout are audit-logged (spec #15/#17). Rate limiting for
brute-force protection is applied by the middleware (auth bucket).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import (
    authenticate,
    hash_password,
    issue_session,
    password_issues,
)
from app.db.database import get_db
from app.db.models import User
from app.db.repositories import AuditRepository, SessionTokenRepository, UserRepository
from app.core.security import get_current_user, _bearer_from

logger = get_logger("prvision.api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


def _session_payload(db: Session, user: User, token: str) -> dict[str, Any]:
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": user.to_dto(),
    }


@router.post("/register", summary="Create an investigator account")
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    if UserRepository.get_by_email(db, body.email) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")

    issues = password_issues(body.password)
    if issues:
        raise HTTPException(
            status_code=422,
            detail=f"Password must contain {', '.join(issues)}")

    # First account bootstraps the admin role; everyone else starts as analyst.
    role = "admin" if UserRepository.count(db) == 0 else "analyst"
    user = UserRepository.create(
        db, email=body.email, password_hash=hash_password(body.password),
        display_name=body.display_name, role=role)
    token = issue_session(db, user=user,
                          user_agent=request.headers.get("user-agent"),
                          ip=request.client.host if request.client else None)
    UserRepository.touch_login(db, user)
    AuditRepository.record(
        db, actor=user.email, action="auth.register", target_type="user",
        target_id=user.id, detail=f"role={role}",
        ip=request.client.host if request.client else None)
    logger.info("Account registered: %s (role=%s)", user.email, role)
    return _session_payload(db, user, token)


@router.post("/login", summary="Log in with email + password")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    user = authenticate(db, str(body.email), body.password)
    if user is None:
        AuditRepository.record(
            db, actor=str(body.email), action="auth.login_failed",
            target_type="user", detail="invalid credentials",
            ip=request.client.host if request.client else None)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = issue_session(db, user=user,
                          user_agent=request.headers.get("user-agent"),
                          ip=request.client.host if request.client else None)
    UserRepository.touch_login(db, user)
    AuditRepository.record(
        db, actor=user.email, action="auth.login", target_type="user", target_id=user.id,
        ip=request.client.host if request.client else None)
    return _session_payload(db, user, token)


@router.post("/logout", summary="Revoke the presented session token")
def logout(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    token = _bearer_from(request)
    if not token:
        raise HTTPException(status_code=401, detail="No session token presented")
    row = SessionTokenRepository.resolve(db, token)
    if row is None:
        # Already invalid/expired — idempotent logout.
        return {"logged_out": True}
    user, _row = row
    SessionTokenRepository.revoke(db, SessionTokenRepository.hash_of(token))
    AuditRepository.record(
        db, actor=user.email, action="auth.logout", target_type="user", target_id=user.id,
        ip=request.client.host if request.client else None)
    return {"logged_out": True}


@router.get("/me", summary="Current authenticated user")
def me(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    token = _bearer_from(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    row = SessionTokenRepository.resolve(db, token)
    if row is None:
        raise HTTPException(status_code=401, detail="Session invalid or expired")
    user, _row = row
    return {"user": user.to_dto()}


# get_current_user is re-exported here for convenient route dependencies elsewhere.
__all__ = ["router", "get_current_user"]
