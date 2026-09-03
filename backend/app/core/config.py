"""PR•VISION application configuration.

All configuration is driven by environment variables (12-factor style) with
sane development defaults. Secrets are never hardcoded; see .env.example.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = parent of backend/
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Central application settings (loaded once, cached)."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    APP_ENV: str = "development"
    APP_VERSION: str = "1.0.0"
    LOG_LEVEL: str = "INFO"

    # ------------------------------------------------------------------ api
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ORIGINS: str = "*"

    # ------------------------------------------------------------- security
    # SECRET_KEY signs nothing by itself (tokens are random + DB-backed) but
    # future cookie/signing needs must not fall back to a hardcoded value.
    # If unset, a random key is generated per-process and a warning is logged.
    SECRET_KEY: str = ""
    AUTH_TOKEN_EXPIRE_HOURS: int = 72
    MIN_PASSWORD_LENGTH: int = 8
    SEED_DEMO_USER: bool = True
    DEMO_USER_EMAIL: str = "demo@prvision.ai"
    DEMO_USER_PASSWORD: str = "DemoVision!2026"

    # Rate limiting (in-process sliding window; disable for tests)
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_API_PER_MINUTE: int = 240
    RATE_LIMIT_AUTH_PER_MINUTE: int = 15
    RATE_LIMIT_VERIFY_PER_MINUTE: int = 12
    RATE_LIMIT_EXPORT_PER_MINUTE: int = 30

    # ------------------------------------------------------------------ db
    DB_ENGINE: str = "mysql"  # "mysql" (production) | "sqlite" (dev/demo fallback)
    MYSQL_HOST: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_DATABASE: str = "prvision"
    MYSQL_USER: str = "prvision"
    MYSQL_PASSWORD: str = ""
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 1800
    SQLITE_PATH: str = "ml/datasets/prvision_demo.db"

    # ------------------------------------------------------------ ingestion
    INGESTION_INTERVAL_SECONDS: int = 30
    INGESTION_ENABLED_ON_STARTUP: bool = False
    # Platforms the scheduler loops over at startup (comma-separated). The
    # big-5 run as keyless web-search harvesters unless credentials exist;
    # mastodon/hackernews run on their free official/public APIs.
    INGESTION_STARTUP_PLATFORMS: str = "demo,x,reddit,instagram,facebook,linkedin,mastodon,hackernews"

    # -------------------------------------------------- web-search harvesting
    # Real public posts for x/reddit/instagram/facebook/linkedin come from the
    # provider sidecar's web search when official API credentials are absent.
    HARVEST_DISCOVERY_INTERVAL_SECONDS: int = 900   # new-topic sweep cadence
    HARVEST_POSTS_PER_DISCOVERY: int = 12            # kept posts per sweep
    HARVEST_SEARCH_NUM: int = 8                      # results per query
    HARVEST_RECENCY_DAYS: int = 7                    # recency filter

    # --------------------------------------------------- public-API platforms
    MASTODON_INSTANCES: str = "mastodon.world,techhub.social,universeodon.com"
    MASTODON_MIN_POLL_SECONDS: int = 180             # be polite to instances
    HACKERNEWS_ENABLED: bool = True                  # free official Firebase API
    HACKERNEWS_MIN_POLL_SECONDS: int = 60

    # ------------------------------------------------------------------ ml
    ML_MODELS_DIR: str = "ml/models"
    FORECAST_HORIZONS: str = "30,60,120"
    PREDICTION_MIN_HISTORY_SNAPSHOTS: int = 3

    # ------------------------------------------------------- scoring weights
    WEIGHT_SPREAD_RISK: float = 0.60
    WEIGHT_MISINFORMATION_RISK: float = 0.40

    # -------------------------------------------------------------- frontend
    DASHBOARD_REFRESH_SECONDS: int = 8

    # ------------------------------------------------------------- platform
    X_BEARER_TOKEN: str = ""
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_USER_AGENT: str = "prvision-early-warning/1.0"
    META_ACCESS_TOKEN: str = ""
    META_INSTAGRAM_ACCOUNT_ID: str = ""
    META_PAGE_ID: str = ""
    LINKEDIN_ACCESS_TOKEN: str = ""
    LINKEDIN_ORGANIZATION_URN: str = ""

    # ------------------------------------------------------- verification
    # Provider sidecar (local Node bridge to z-ai-web-dev-sdk: web search,
    # page reader, LLM, vision). When unreachable the evidence engine falls
    # back to keyless public providers and honest DISABLED states.
    SIDECAR_URL: str = "http://127.0.0.1:8787"
    SIDECAR_ENABLED: bool = True
    SIDECAR_TIMEOUT_SECONDS: float = 55.0

    # Fact-check / news API keys (optional; providers report DISABLED without)
    GOOGLE_FACTCHECK_API_KEY: str = ""
    NEWSAPI_KEY: str = ""

    # URL fetching
    VERIFY_FETCH_TIMEOUT_SECONDS: float = 20.0
    VERIFY_FETCH_MAX_BYTES: int = 5_000_000
    VERIFY_MAX_CLAIMS: int = 12
    VERIFY_MAX_EVIDENCE_PER_CLAIM: int = 6

    # Upload handling
    VERIFY_UPLOAD_DIR: str = "ml/uploads"
    VERIFY_UPLOAD_MAX_MB: int = 25

    # ---------------------------------------------------------- validators
    @field_validator("DB_ENGINE")
    @classmethod
    def _validate_engine(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"mysql", "sqlite"}:
            raise ValueError("DB_ENGINE must be 'mysql' or 'sqlite'")
        return v

    @field_validator("WEIGHT_SPREAD_RISK", "WEIGHT_MISINFORMATION_RISK")
    @classmethod
    def _validate_weights(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("priority weights must be within [0, 1]")
        return v

    @field_validator("MIN_PASSWORD_LENGTH", "AUTH_TOKEN_EXPIRE_HOURS")
    @classmethod
    def _validate_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be >= 1")
        return v

    # ----------------------------------------------------------- properties
    @property
    def horizons(self) -> List[int]:
        return [int(h) for h in self.FORECAST_HORIZONS.split(",") if h.strip()]

    @property
    def cors_origin_list(self) -> List[str]:
        origins = [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]
        return origins or ["*"]

    @property
    def weight_sum(self) -> float:
        return self.WEIGHT_SPREAD_RISK + self.WEIGHT_MISINFORMATION_RISK

    @property
    def sqlalchemy_url(self) -> str:
        """Build the SQLAlchemy connection URL for the configured engine."""
        if self.DB_ENGINE == "sqlite":
            db_path = Path(self.SQLITE_PATH)
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite+pysqlite:///{db_path}"
        return (
            f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}?charset=utf8mb4"
        )

    @property
    def models_dir(self) -> Path:
        p = Path(self.ML_MODELS_DIR)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def is_platform_configured(self, platform: str) -> bool:
        """Whether a platform can ingest data right now.

        Big-5 platforms need official credentials; mastodon/hackernews run on
        keyless public APIs (they only need their endpoints enabled).
        """
        checks = {
            "x": bool(self.X_BEARER_TOKEN),
            "reddit": bool(self.REDDIT_CLIENT_ID and self.REDDIT_CLIENT_SECRET),
            "instagram": bool(self.META_ACCESS_TOKEN and self.META_INSTAGRAM_ACCOUNT_ID),
            "facebook": bool(self.META_ACCESS_TOKEN and self.META_PAGE_ID),
            "linkedin": bool(self.LINKEDIN_ACCESS_TOKEN),
            "youtube": bool(self.GOOGLE_FACTCHECK_API_KEY),  # Google cloud key gates YouTube Data API too
            "mastodon": bool(self.MASTODON_INSTANCES.strip()),
            "hackernews": bool(self.HACKERNEWS_ENABLED),
            "demo": True,
        }
        return checks.get(platform, False)

    @property
    def startup_platforms(self) -> list[str]:
        """Parsed INGESTION_STARTUP_PLATFORMS (deduped, order-preserving)."""
        seen: list[str] = []
        for part in self.INGESTION_STARTUP_PLATFORMS.split(","):
            name = part.strip().lower()
            if name and name not in seen:
                seen.append(name)
        return seen or ["demo"]

    @property
    def upload_dir(self) -> Path:
        p = Path(self.VERIFY_UPLOAD_DIR)
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor used across the application."""
    return Settings()


settings = get_settings()
