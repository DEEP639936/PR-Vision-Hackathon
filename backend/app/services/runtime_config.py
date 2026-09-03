"""Runtime provider-key configuration (deployment settings).

Lets the deployment owner paste evidence-provider API keys from the UI:
the key is validated, persisted to the project .env file (comments preserved),
applied to the running settings object, and immediately probed live so the
provider panel shows the REAL resulting state (CONNECTED / AUTH_REQUIRED / ...).

Secrets are never returned by the API — only a masked hint (last 4 chars).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from app.core.config import PROJECT_ROOT, settings
from app.core.logging import get_logger
from app.evidence.providers import _reset_probe_cache

logger = get_logger("prvision.services.runtime_config")

# provider name -> (settings attribute, human label)
PROVIDER_ENV_KEYS: dict[str, tuple[str, str]] = {
    "google_factcheck": ("GOOGLE_FACTCHECK_API_KEY", "Google Fact Check Tools API"),
    "newsapi": ("NEWSAPI_KEY", "NewsAPI"),
}

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


class ProviderKeyError(ValueError):
    """Invalid provider name or key payload."""


def _env_path(env_path: Optional[Path] = None) -> Path:
    return Path(env_path) if env_path else DEFAULT_ENV_PATH


def _attr(provider: str) -> str:
    if provider not in PROVIDER_ENV_KEYS:
        raise ProviderKeyError(f"unknown provider '{provider}'")
    return PROVIDER_ENV_KEYS[provider][0]


def _upsert_env_line(env_file: Path, attr: str, value: str) -> None:
    """Write KEY=value into .env, preserving all other lines and comments."""
    line_re = re.compile(rf"^{re.escape(attr)}\s*=.*$", re.MULTILINE)           # real line
    commented_re = re.compile(rf"^#\s*{re.escape(attr)}\s*=.*$", re.MULTILINE)  # commented template
    replacement = f"{attr}={value}"
    if env_file.exists():
        content = env_file.read_text(encoding="utf-8")
        if line_re.search(content):
            content = line_re.sub(replacement, content, count=1)
        elif commented_re.search(content):
            content = commented_re.sub(replacement, content, count=1)
        else:
            content = content.rstrip("\n") + "\n" + replacement + "\n"
        tmp = env_file.with_suffix(".env.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, env_file)
    else:
        env_file.write_text(
            "# PR•VISION — environment configuration (managed keys appended)\n"
            f"{replacement}\n", encoding="utf-8")
    try:
        os.chmod(env_file, 0o600)  # best-effort: secrets on disk stay private
    except OSError:
        pass


def set_provider_key(provider: str, key: str, env_path: Optional[Path] = None) -> str:
    """Persist + apply a provider API key at runtime. Returns the settings attr."""
    key = (key or "").strip()
    if not key:
        raise ProviderKeyError("key must not be empty")
    if len(key) > 256 or any(c.isspace() for c in key):
        raise ProviderKeyError("key must be a single token without whitespace")
    attr = _attr(provider)
    _upsert_env_line(_env_path(env_path), attr, key)
    setattr(settings, attr, key)          # applied to the running process
    _reset_probe_cache()                  # force a fresh live probe
    logger.info("provider key updated: %s (state will be re-probed)", provider)
    return attr


def clear_provider_key(provider: str, env_path: Optional[Path] = None) -> str:
    """Remove a provider key (env line emptied + runtime cleared)."""
    attr = _attr(provider)
    _upsert_env_line(_env_path(env_path), attr, "")
    setattr(settings, attr, "")
    _reset_probe_cache()
    logger.info("provider key cleared: %s", provider)
    return attr


def masked_key(provider: str) -> Optional[str]:
    """Masked hint of the stored key, e.g. '••••9f2a' — never the full secret."""
    attr = _attr(provider)
    value = getattr(settings, attr, "") or ""
    if not value:
        return None
    tail = value[-4:] if len(value) >= 8 else "••••"
    return f"••••{tail}" if len(value) >= 8 else "••••"


def provider_label(provider: str) -> str:
    return PROVIDER_ENV_KEYS[provider][1]
