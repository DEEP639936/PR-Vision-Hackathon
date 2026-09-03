#!/usr/bin/env python3
"""Train PR•VISION models (XGBoost share forecasting + misinformation risk).

Builds training data from metric/feature snapshots in the database, applies a
chronological split (spec #20), evaluates against the velocity baseline, and
records versioned artifacts + metrics in the model registry.

Usage (from project root or backend/):
    python scripts/train_models.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.config import settings  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402

configure_logging("INFO")
logger = get_logger("prvision.scripts.train")


def main() -> int:
    from sqlalchemy.orm import sessionmaker
    from app.db.database import engine
    from app.ml.inference import ModelManager
    from app.ml.training import train_forecast_model, train_misinformation_model

    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        summary: dict = {"horizons": {}, "misinformation": None}
        for horizon in settings.horizons:
            result = train_forecast_model(db, horizon, models_dir=settings.models_dir)
            summary["horizons"][str(horizon)] = result

        summary["misinformation"] = train_misinformation_model(models_dir=settings.models_dir)

        # Refresh the portable exported-weights bundle so slim publish runtimes
        # (no numpy/xgboost) serve the NEW models too. Best-effort: a host
        # without the export deps still keeps its previous bundle.
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "export_portable_models",
                Path(__file__).resolve().parent / "export_portable_models.py")
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            export_args = sys.argv
            sys.argv = ["export_portable_models.py",
                        "--models-dir", str(settings.models_dir)]
            try:
                summary["portable_export_exit"] = _mod.main()
            finally:
                sys.argv = export_args
        except Exception as exc:  # export must never fail training
            summary["portable_export_exit"] = f"skipped: {exc.__class__.__name__}: {exc}"

        reload_info = ModelManager.instance().load_models()
        summary["reloaded"] = reload_info

        print("\n" + "=" * 72)
        print("PR•VISION TRAINING SUMMARY")
        print("=" * 72)
        print(json.dumps(summary, indent=2, default=str))
        print("=" * 72)

        trained = [h for h, r in summary["horizons"].items() if r.get("status") == "trained"]
        skipped = [h for h, r in summary["horizons"].items() if r.get("status") == "skipped"]
        if skipped:
            logger.warning("Horizons skipped (need ≥60 training rows each): %s — "
                           "generate more demo data and re-run.", ", ".join(skipped))
        logger.info("Done. Trained horizons: %s", trained or "none")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
