"""PR•VISION — FastAPI application factory.

Serves:
    /api/*          REST API (OpenAPI docs at /docs)
    /               the vanilla HTML/CSS/JS operations dashboard
    /assets/*       frontend static files

Startup (lifespan): loads ML models, registers DataSourceStatus rows, and
optionally auto-starts demo ingestion (INGESTION_ENABLED_ON_STARTUP=true).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import (alerts, auth, cases, dashboard, export, health, ingestion,
                            ml, platforms, posts, predictions, verify)
from app.api.routes import settings as settings_routes
from app.core.config import PROJECT_ROOT, settings
from app.core.logging import configure_logging, get_logger
from app.core.headers import SecurityHeadersMiddleware
from app.core.ratelimit import RateLimitMiddleware
from app.db.database import engine
from app.db.models import Base
from app.db.repositories import DataSourceStatusRepository
from app.ml.inference import ModelManager
from app.connectors import SUPPORTED_PLATFORMS, close_all

configure_logging(settings.LOG_LEVEL)
logger = get_logger("prvision.main")

FRONTEND_DIR = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---------------------------------------------------------
    Base.metadata.create_all(bind=engine)  # dev convenience; Alembic is canonical
    manager = ModelManager.instance()
    manager.load_models()

    # Orphaned verification jobs: a restart (deploy, crash, sandbox recycle)
    # leaves rows stuck at queued/running forever. Mark them honestly — they
    # were interrupted, and no worker will resume them.
    from app.db.database import session_scope
    from app.db.models import VerificationJob
    from datetime import datetime, timezone as _tz
    with session_scope() as db:
        orphans = (db.query(VerificationJob)
                   .filter(VerificationJob.status.in_(("queued", "running"))).all())
        for job in orphans:
            job.status = "failed"
            job.error = "Interrupted by a service restart — please resubmit."
            job.finished_at = datetime.now(_tz.utc)
        if orphans:
            logger.warning("Marked %d orphaned verification job(s) as failed on startup",
                           len(orphans))

    with session_scope() as db:
        for platform in SUPPORTED_PLATFORMS:
            DataSourceStatusRepository.upsert(
                db, platform,
                status="healthy" if settings.is_platform_configured(platform) else "not_configured")

    if settings.INGESTION_ENABLED_ON_STARTUP:
        from app.services.ingestion_service import scheduler
        await scheduler.start(platforms=settings.startup_platforms)

    _seed_demo_user()
    _register_model_versions()

    logger.info("PR•VISION %s ready (env=%s, db=%s)", settings.APP_VERSION, settings.APP_ENV, settings.DB_ENGINE)
    yield
    # --- shutdown --------------------------------------------------------
    from app.services.ingestion_service import scheduler as sched
    await sched.stop()
    await close_all()
    logger.info("PR•VISION shutdown complete")


def _seed_demo_user() -> None:
    """Seed the documented demo analyst account (hackathon / first-run UX)."""
    if not settings.SEED_DEMO_USER:
        return
    import secrets as _secrets

    from app.core.security import hash_password
    from app.db.database import session_scope
    from app.db.repositories import AuditRepository, UserRepository
    try:
        with session_scope() as db:
            if UserRepository.get_by_email(db, settings.DEMO_USER_EMAIL) is not None:
                return
            user = UserRepository.create(
                db, email=settings.DEMO_USER_EMAIL,
                password_hash=hash_password(settings.DEMO_USER_PASSWORD),
                display_name="Demo Analyst", role="admin")
            AuditRepository.record(
                db, actor="system", action="auth.seed_demo_user",
                target_type="user", target_id=user.id,
                detail="seeded default demo analyst account")
        logger.info("Seeded demo analyst account: %s", settings.DEMO_USER_EMAIL)
    except Exception:
        logger.exception("Demo user seeding failed (non-fatal)")


def _register_model_versions() -> None:
    """Mirror the file-based model registry into the model_versions table."""
    import json as _json

    from app.db.database import session_scope
    from app.db.repositories import ModelVersionRepository
    registry = settings.models_dir / "registry.json"
    if not registry.exists():
        return
    try:
        entries = _json.loads(registry.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    rows = entries.get("models") if isinstance(entries, dict) else entries
    if not isinstance(rows, list):
        return
    try:
        with session_scope() as db:
            for row in rows[:64]:
                if not isinstance(row, dict) or not row.get("name") or not row.get("version"):
                    continue
                ModelVersionRepository.register(
                    db, name=str(row["name"]), version=str(row["version"]),
                    task=str(row.get("task") or ("forecast" if "forecast" in str(row.get("name", "")) else "misinformation")),
                    horizon_minutes=row.get("horizon_minutes"),
                    artifact_path=row.get("path"),
                    metrics=row.get("metrics") or {})
    except Exception:
        logger.exception("Model registry mirror failed (non-fatal)")


app = FastAPI(
    title="PR•VISION API",
    description=(
        "AI-Powered Multimodal Misinformation Intelligence, Verification & Early-Warning Platform.\n\n"
        "**Two engines, one platform:**\n\n"
        "1. **Verification** — submit URLs, articles, images, screenshots, PDFs, DOCX, CSV or text; "
        "PR•VISION extracts claims, retrieves external evidence & professional fact-checks, runs media "
        "forensics (OCR, metadata, manipulation heuristics), performs deterministic numerical checks, and "
        "fuses everything into an explainable verdict with per-claim evidence citations.\n\n"
        "2. **Early warning** — monitors social posts, forecasts additional shares (XGBoost, 30/60/120 min) "
        "and produces the Intervention Priority Score (0-100) for human moderators.\n\n"
        "⚠️ PR•VISION is decision-support for human investigators. It never issues automated truth verdicts; "
        "every conclusion carries evidence, confidence and reasoning."
    ),
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS (config-driven; "*" only in dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
# Security headers + in-process rate limiting (spec #17)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)

# API routers
app.include_router(health.router, prefix="/api")
app.include_router(platforms.router, prefix="/api")
app.include_router(posts.router, prefix="/api")
app.include_router(predictions.router, prefix="/api")
app.include_router(dashboard.router, prefix="/api")
app.include_router(ingestion.router, prefix="/api")
app.include_router(ml.router, prefix="/api")
app.include_router(verify.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(cases.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(settings_routes.router, prefix="/api")

# Frontend (vanilla HTML/CSS/JS) — no-cache so asset updates land immediately
class NoCacheStaticFiles(StaticFiles):
    """StaticFiles with Cache-Control: no-cache (revalidate every load)."""

    def file_response(self, *args, **kwargs):  # noqa: ANN002, ANN003
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if (FRONTEND_DIR / "index.html").exists():
    app.mount("/css", NoCacheStaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")
    app.mount("/js", NoCacheStaticFiles(directory=str(FRONTEND_DIR / "js")), name="js")
    if (FRONTEND_DIR / "assets").exists():
        app.mount("/assets", NoCacheStaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    def _page(name: str):
        return FileResponse(FRONTEND_DIR / name, headers={"Cache-Control": "no-cache"})

    def _register_page(path: str, filename: str) -> None:
        """Register a page at both `path` and `path/` (trailing-slash variant).

        Starlette's redirect_slashes would answer the trailing-slash request
        with a 307 whose absolute Location is rebuilt from the request
        Host/scheme. Behind a reverse proxy / preview gateway that URL can
        point at a host:port the browser cannot reach (observed in the wild:
        ERR_CONNECTION_TIMED_OUT immediately after a successful login).
        Serving both variants directly removes the redirect entirely.
        """

        async def _handler():
            return _page(filename)

        app.add_api_route(path, _handler, methods=["GET"], include_in_schema=False)
        if not path.endswith("/"):
            app.add_api_route(path + "/", _handler, methods=["GET"], include_in_schema=False)

    _register_page("/", "landing.html")
    _register_page("/dashboard", "index.html")
    _register_page("/verify", "verify.html")
    _register_page("/login", "login.html")
    _register_page("/register", "register.html")
    _register_page("/cases", "cases.html")
    _register_page("/report/{job_id}", "report.html")
