"""Unit tests for runtime provider-key persistence (runtime_config service)."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.evidence.providers import _probe_cache
from app.services import runtime_config


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# PR•VISION — environment configuration\n"
        "# comment preserved\n"
        "GOOGLE_FACTCHECK_API_KEY=\n"
        "NEWSAPI_KEY=\n",
        encoding="utf-8")
    monkeypatch.setattr(runtime_config, "DEFAULT_ENV_PATH", env_file)
    saved = (settings.GOOGLE_FACTCHECK_API_KEY, settings.NEWSAPI_KEY)
    yield env_file
    settings.GOOGLE_FACTCHECK_API_KEY, settings.NEWSAPI_KEY = saved
    _probe_cache.clear()


def test_set_provider_key_persists_and_applies(tmp_env):
    attr = runtime_config.set_provider_key("google_factcheck", "test-key-12345678")
    assert attr == "GOOGLE_FACTCHECK_API_KEY"
    assert settings.GOOGLE_FACTCHECK_API_KEY == "test-key-12345678"
    content = tmp_env.read_text(encoding="utf-8")
    assert "GOOGLE_FACTCHECK_API_KEY=test-key-12345678" in content
    assert "# comment preserved" in content          # comments survive
    assert "NEWSAPI_KEY=" in content                  # other keys survive


def test_set_provider_key_updates_existing_line(tmp_env):
    runtime_config.set_provider_key("newsapi", "first-key-00000001")
    runtime_config.set_provider_key("newsapi", "second-key-00000002")
    content = tmp_env.read_text(encoding="utf-8")
    assert "NEWSAPI_KEY=second-key-00000002" in content
    assert "first-key" not in content


def test_set_provider_key_rejects_bad_input(tmp_env):
    with pytest.raises(runtime_config.ProviderKeyError):
        runtime_config.set_provider_key("google_factcheck", "   ")
    with pytest.raises(runtime_config.ProviderKeyError):
        runtime_config.set_provider_key("google_factcheck", "two words")
    with pytest.raises(runtime_config.ProviderKeyError):
        runtime_config.set_provider_key("not_a_provider", "x")


def test_clear_provider_key(tmp_env):
    runtime_config.set_provider_key("google_factcheck", "gone-key-12345678")
    runtime_config.clear_provider_key("google_factcheck")
    assert settings.GOOGLE_FACTCHECK_API_KEY == ""
    assert "GOOGLE_FACTCHECK_API_KEY=" in tmp_env.read_text(encoding="utf-8")


def test_masked_key(tmp_env):
    assert runtime_config.masked_key("google_factcheck") is None
    runtime_config.set_provider_key("google_factcheck", "AIzaSyD-1234567890abcd")
    assert runtime_config.masked_key("google_factcheck") == "••••abcd"
    assert "AIza" not in runtime_config.masked_key("google_factcheck")
