"""Shared pytest fixtures — isolated SQLite DB per test session."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# Must be set BEFORE app modules import (settings reads env at import).
os.environ["DB_ENGINE"] = "sqlite"
os.environ["SQLITE_PATH"] = str(PROJECT_ROOT / "ml" / "datasets" / "test_prvision.db")
os.environ["APP_ENV"] = "test"
os.environ["INGESTION_ENABLED_ON_STARTUP"] = "false"
# Keep public-API platforms offline in tests: no instance/endpoint probes from
# the platforms listing (their parsing/normalization is unit-tested with
# mocked payloads in tests/unit/).
os.environ["MASTODON_INSTANCES"] = ""
os.environ["HACKERNEWS_ENABLED"] = "false"
# The suite makes many auth calls against ONE shared session-scoped client;
# the 15/min auth window would otherwise make the suite order-dependent.
# (The limiter's own behaviour is still unit-tested in test_security.py with
# a dedicated app and monkeypatched limits.)
os.environ["RATE_LIMIT_ENABLED"] = "false"


@pytest.fixture(scope="session")
def app():
    """FastAPI app with lifespan executed against the test DB."""
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:  # `with` triggers lifespan
        yield client


@pytest.fixture()
def db_session():
    from app.db.database import SessionLocal
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def demo_posts(db_session):
    """Two demo posts with backfilled snapshots (same pipeline as runtime)."""
    import asyncio

    from app.services.demo_service import DemoService

    async def _gen():
        return await DemoService.generate_posts(
            db_session, num_posts=2, archetypes=["normal", "suspicious_viral"], score=True)

    created = asyncio.run(_gen())
    db_session.commit()  # make the posts visible to the app's own sessions
    yield created


@pytest.fixture(scope="session")
def trained_models(tmp_path_factory):
    """Train lightweight models on the test DB (after demo posts exist)."""
    # NOTE: intentionally lazy — see test_ml.py which invokes the pipeline.
    yield None
