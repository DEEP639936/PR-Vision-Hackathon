"""SQLAlchemy database engine/session management.

Production target is local MySQL (connection pooling, recycle, pre-ping);
SQLite is supported as an explicit dev/demo fallback (DB_ENGINE=sqlite) so the
entire pipeline can run on machines without a MySQL server (e.g. CI).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("prvision.db")


def _build_engine() -> Engine:
    url = settings.sqlalchemy_url
    if settings.DB_ENGINE == "mysql":
        engine = create_engine(
            url,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_recycle=settings.DB_POOL_RECYCLE,
            pool_pre_ping=True,  # survive MySQL server restarts / stale conns
            future=True,
        )
    else:
        # timeout: sqlite3 busy-wait (seconds) before giving up on a lock —
        # lets ingestion writes and API traffic coexist without spurious
        # "database is locked" failures.
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")      # readers don't block the writer
            cursor.execute("PRAGMA synchronous=NORMAL")    # safe with WAL, much faster
            cursor.execute("PRAGMA busy_timeout=30000")    # ms, belt-and-braces with timeout
            cursor.close()

    logger.info("Database engine initialised (engine=%s)", settings.DB_ENGINE)
    return engine


engine: Engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — one session per request.

    Commits on successful request completion so mutating endpoints (demo
    generation, predictions, ingestion updates) persist; rolls back on error.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Contextual session with commit/rollback semantics (services/tests)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database_connection() -> bool:
    """Cheap liveness probe used by /api/health (reflects real state)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - depends on live DB
        logger.error("Database health check failed: %s", exc)
        return False
