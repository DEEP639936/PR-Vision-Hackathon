"""Security primitives — password hashing, bearer tokens, auth dependencies.

Deliberately stdlib-only (no passlib/python-jose dependency risk):
- Passwords: PBKDF2-HMAC-SHA256, 390k iterations, 16-byte random salt,
  constant-time verification. Format: pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>
- Tokens: 32-byte `secrets.token_urlsafe`; ONLY the SHA-256 hash is stored,
  so a database dump cannot be replayed as a valid session.
- Dependencies: get_current_user (optional) / require_user (mandatory) /
  require_admin, reading `Authorization: Bearer <token>`.

External content fetched by the verification pipeline is untrusted data and
is never executed or interpreted as instructions.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import get_db
from app.db.models import User
from app.db.repositories import SessionTokenRepository, UserRepository

_PBKDF2_ITERATIONS = 390_000
_TOKEN_BYTES = 32


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    pwd = (password or "").encode("utf-8")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pwd, salt, _PBKDF2_ITERATIONS)
    import base64
    return (
        f"pbkdf2_sha256${_PBKDF2_ITERATIONS}$"
        f"{base64.urlsafe_b64encode(salt).decode().rstrip('=')}$"
        f"{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = (stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        import base64
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        salt = base64.urlsafe_b64decode(pad(salt_b64))
        expected = base64.urlsafe_b64decode(pad(hash_b64))
        actual = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def password_issues(password: str) -> list[str]:
    """Policy: >= MIN_PASSWORD_LENGTH, at least one letter and one digit."""
    issues: list[str] = []
    if len(password or "") < settings.MIN_PASSWORD_LENGTH:
        issues.append(f"at least {settings.MIN_PASSWORD_LENGTH} characters")
    if not any(c.isalpha() for c in password or ""):
        issues.append("at least one letter")
    if not any(c.isdigit() for c in password or ""):
        issues.append("at least one digit")
    return issues


# ------------------------------------------------------------------------------- tokens
def new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# -------------------------------------------------------------------------- dependencies
def _bearer_from(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Optional auth — returns None for anonymous callers (never raises)."""
    token = _bearer_from(request)
    if not token:
        return None
    resolved = SessionTokenRepository.resolve(db, token)
    return resolved[0] if resolved else None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Mandatory auth — 401 when the caller has no valid session."""
    token = _bearer_from(request)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    resolved = SessionTokenRepository.resolve(db, token)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Session invalid or expired — log in again",
                            headers={"WWW-Authenticate": "Bearer"})
    return resolved[0]


def require_admin(user: User = Depends(require_user)) -> User:
    if (user.role or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return user


# ------------------------------------------------------------------------------- helpers
def utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def issue_session(db: Session, *, user: User, user_agent: str | None, ip: str | None) -> str:
    token = new_token()
    SessionTokenRepository.issue(
        db, user_id=user.id, token=token,
        expires_hours=settings.AUTH_TOKEN_EXPIRE_HOURS,
        user_agent=user_agent, ip=ip)
    return token


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = UserRepository.get_by_email(db, email)
    if user is None or not user.is_active:
        # Burn comparable time to blunt user-enumeration timing signals.
        verify_password(password or "", hash_password("timing-equalizer"))
        return None
    if not verify_password(password or "", user.password_hash):
        return None
    return user
