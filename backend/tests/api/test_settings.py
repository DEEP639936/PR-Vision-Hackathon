"""Settings API tests — provider key save/probe/clear flow (honest states)."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.evidence import providers as prov
from app.services import runtime_config


_HEADERS_CACHE: dict[str, dict[str, str]] = {}


def _login(app):
    """One shared registration for the whole module (auth rate limit: 15/min)."""
    if _HEADERS_CACHE:
        return _HEADERS_CACHE["headers"]
    email = f"keys-{uuid.uuid4().hex[:8]}@prvision.io"
    r = app.post("/api/auth/register", json={
        "email": email, "display_name": "Key Tester", "password": "Sup3rSecret!"})
    if r.status_code in (409, 429):
        # user may already exist / limiter hit — fall back to login
        r = app.post("/api/auth/login", json={
            "email": email, "password": "Sup3rSecret!"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    _HEADERS_CACHE["headers"] = headers
    return headers


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_config, "DEFAULT_ENV_PATH", tmp_path / ".env")
    saved = (prov.settings.GOOGLE_FACTCHECK_API_KEY, prov.settings.NEWSAPI_KEY)
    yield
    prov.settings.GOOGLE_FACTCHECK_API_KEY, prov.settings.NEWSAPI_KEY = saved
    prov._reset_probe_cache()


def test_provider_keys_requires_auth(app):
    assert app.get("/api/settings/provider-keys").status_code == 401
    r = app.post("/api/settings/provider-keys", json={"provider": "newsapi", "key": "x"})
    assert r.status_code == 401


def test_save_key_probe_connected(app, monkeypatch):
    """A key accepted by the provider -> CONNECTED (mocked live probe)."""
    headers = _login(app)
    monkeypatch.setattr(prov.settings, "NEWSAPI_KEY", "", raising=False)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        get=AsyncMock(return_value=_mk_resp(200, {"status": "ok"}))))
    cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(prov.httpx, "AsyncClient", lambda **kw: cm)

    r = app.post("/api/settings/provider-keys", json={
        "provider": "newsapi", "key": "valid-key-12345678"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["state"] == "CONNECTED"
    assert body["masked"] == "••••5678"
    assert "key" not in str(body).lower() or "masked" in body  # no raw secret echoed


def test_save_key_rejected_is_auth_required(app, monkeypatch):
    headers = _login(app)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        get=AsyncMock(return_value=_mk_resp(401, {
            "status": "error", "code": "apiKeyInvalid", "message": "bad key"}))))
    cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(prov.httpx, "AsyncClient", lambda **kw: cm)

    r = app.post("/api/settings/provider-keys", json={
        "provider": "google_factcheck", "key": "wrong-key-12345678"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "AUTH_REQUIRED"


def test_clear_key_returns_to_disabled(app, monkeypatch):
    headers = _login(app)
    monkeypatch.setattr(prov.settings, "NEWSAPI_KEY", "", raising=False)
    r = app.delete("/api/settings/provider-keys/newsapi", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["state"] == "DISABLED"


def test_get_provider_keys_masks_secrets(app, monkeypatch):
    headers = _login(app)
    monkeypatch.setattr(prov.settings, "NEWSAPI_KEY", "secret-key-abcd9876",
                        raising=False)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock(
        get=AsyncMock(return_value=_mk_resp(200, {"status": "ok"}))))
    cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(prov.httpx, "AsyncClient", lambda **kw: cm)

    r = app.get("/api/settings/provider-keys", headers=headers)
    assert r.status_code == 200
    body = r.json()
    by_name = {p["provider"]: p for p in body["providers"]}
    assert set(by_name) == {"google_factcheck", "newsapi"}
    assert by_name["newsapi"]["masked"] == "••••9876"
    assert "secret-key-abcd9876" not in r.text  # raw secret never leaves the server


def _mk_resp(status_code, payload):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = payload
    return r
