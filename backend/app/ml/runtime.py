"""ML runtime capability probe (publish-runtime support).

The one-click platform publish pipeline installs Python dependencies into the
deployment artifact from the ROOT requirements.txt. That manifest intentionally
ships a light runtime (fast install + small artifact) so the platform build
finishes in minutes. The heavy numeric stack (numpy / scikit-learn / xgboost /
scipy, ~500 MB installed) is part of the FULL runtime manifest
(``pr-vision/backend/requirements.txt``) used by Docker / VPS / Railway
deployments and the sandbox.

This module reports — once, at import — which numeric libraries are actually
importable on THIS host, so the ML layer can degrade HONESTLY instead of
crashing the whole API at boot:

    runtime available   -> models load exactly as before (no behavior change)
    runtime missing     -> model artifacts stay unloaded; forecasts fall back
                           to the transparent velocity baseline and the
                           misinformation scorer to the heuristic layer, with
                           the real reason surfaced in /api/health,
                           /api/ml/status and every prediction payload.
"""
from __future__ import annotations

import importlib.util
from typing import Any

# module name -> distribution name as users would install it
_PROBE_MODULES: dict[str, str] = {
    "numpy": "numpy",
    "joblib": "joblib",
    "sklearn": "scikit-learn",
    "xgboost": "xgboost",
}


def probe_ml_runtime() -> dict[str, Any]:
    """Return {"available": bool, "missing": [dist names], "reason": str|None}."""
    missing = [pkg for mod, pkg in _PROBE_MODULES.items()
               if importlib.util.find_spec(mod) is None]
    if not missing:
        return {"available": True, "missing": [], "reason": None}
    reason = (
        "ML numeric runtime not installed on this host (missing: "
        + ", ".join(missing)
        + "). Trained models are served through the portable exported-weights "
        "engine (app/ml/portable.py, parity-verified); (re-)training and live "
        "model loading need the full runtime — deploy via Docker (DEPLOYMENT.md)."
    )
    return {"available": False, "missing": missing, "reason": reason}


ML_RUNTIME: dict[str, Any] = probe_ml_runtime()
