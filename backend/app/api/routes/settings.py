"""Deployment settings API — evidence-provider API keys.

    GET    /api/settings/provider-keys          masked key status + live state (auth)
    POST   /api/settings/provider-keys          save key + real probe -> honest state (auth)
    DELETE /api/settings/provider-keys/{p}      clear key -> DISABLED again (auth)

The state returned by POST is the provider's REAL state after a live probe
call — the API never reports CONNECTED unless the provider itself accepted
the key. Keys are persisted to the project .env (chmod 600) and applied to
the running process without restart.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.security import require_user
from app.db.models import User
from app.evidence.providers import EvidenceRetriever
from app.services import runtime_config
from app.services.runtime_config import ProviderKeyError

router = APIRouter(prefix="/settings", tags=["settings"])


class ProviderKeyPayload(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    key: str = Field(min_length=1, max_length=256)


async def _provider_health(provider: str) -> tuple[str, str]:
    retriever = EvidenceRetriever()
    if provider == "google_factcheck":
        st = await retriever.factcheck.health()
    else:
        st = await retriever.providers[2].health()
    return st.state, (st.detail or "")


@router.get("/provider-keys", summary="Masked key status for keyed providers (never returns secrets)")
async def get_provider_keys(user: User = Depends(require_user)) -> dict[str, Any]:
    out = []
    for provider in runtime_config.PROVIDER_ENV_KEYS:
        state, detail = await _provider_health(provider)
        out.append({
            "provider": provider,
            "label": runtime_config.provider_label(provider),
            "key_set": bool(runtime_config.masked_key(provider)),
            "masked": runtime_config.masked_key(provider),
            "state": state,
            "detail": detail,
        })
    return {"providers": out,
            "note": "States are live-probed. CONNECTED means the provider accepted the key."}


@router.post("/provider-keys", summary="Save a provider API key and probe it live")
async def save_provider_key(payload: ProviderKeyPayload,
                            user: User = Depends(require_user)) -> dict[str, Any]:
    try:
        runtime_config.set_provider_key(payload.provider, payload.key)
    except ProviderKeyError as exc:
        return {"ok": False, "provider": payload.provider, "error": str(exc)}
    state, detail = await _provider_health(payload.provider)
    return {"ok": True, "provider": payload.provider,
            "masked": runtime_config.masked_key(payload.provider),
            "state": state, "detail": detail,
            "note": "Key saved to .env on the server. State below is from a real probe call."}


@router.delete("/provider-keys/{provider}", summary="Clear a provider API key")
async def clear_provider_key(provider: str,
                             user: User = Depends(require_user)) -> dict[str, Any]:
    try:
        runtime_config.clear_provider_key(provider)
    except ProviderKeyError as exc:
        return {"ok": False, "provider": provider, "error": str(exc)}
    state, detail = await _provider_health(provider)
    return {"ok": True, "provider": provider, "state": state, "detail": detail}
