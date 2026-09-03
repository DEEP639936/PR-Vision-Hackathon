#!/usr/bin/env python3
"""Register verification-engine models in the model registry (spec #51).

The verification pipeline combines learned components (claim typing, stance
scoring) with deterministic engines (source ranking, evidence fusion, image
forensics, numerical checks). Each is registered here with version, type,
dataset description, thresholds and status so the /api/ml/status view and the
docs reflect the full model inventory.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REGISTRY = Path(__file__).resolve().parents[1] / "ml" / "models" / "registry.json"

VERIFICATION_MODELS = [
    {
        "model_name": "claim-extraction-v1",
        "model_type": "claim_extraction",
        "version": "1.0.0",
        "trained_at": None,
        "dataset": "deterministic heuristics + optional LLM refinement (hybrid)",
        "type_": "rule_based_hybrid",
        "metrics": {"design_coverage": "FACTUAL/OPINION/PREDICTION/QUESTION/SATIRE/EMOTIONAL"},
        "thresholds": {"min_claim_words": 5, "max_claims_per_content": 12},
        "status": "active",
    },
    {
        "model_name": "claim-risk-v1",
        "model_type": "stance_and_fusion",
        "version": "1.0.0",
        "trained_at": None,
        "dataset": "lexical stance cues over retrieved evidence",
        "type_": "evidence_fusion",
        "metrics": {"verdict_taxonomy_size": 10},
        "thresholds": {"strong_contra_ratio": 1.6, "strong_support_min": 0.8},
        "status": "active",
    },
    {
        "model_name": "source-quality-v1",
        "model_type": "source_ranking",
        "version": "1.0.0",
        "dataset": "transparent contextual signals (identity, transparency, recency, agreement)",
        "type_": "signal_ranking",
        "metrics": {"signals_catalogued": 12},
        "thresholds": {"squash_steepness": 2.2},
        "status": "active",
    },
    {
        "model_name": "image-forensics-v1",
        "model_type": "media_forensics",
        "version": "1.0.0",
        "trained_at": None,
        "dataset": "n/a — heuristic detectors (resave diff, noise inconsistency, sharpness disparity) + tesseract OCR + EXIF",
        "type_": "heuristic_ensemble",
        "metrics": {"note": "signals are review indicators, never proof"},
        "thresholds": {"resave_diff": 6.0, "noise_dispersion": 0.9, "edge_disparity": 1.6},
        "status": "active",
    },
    {
        "model_name": "numerical-factcheck-v1",
        "model_type": "numerical_verification",
        "version": "1.0.0",
        "dataset": "deterministic Python arithmetic — LLM never performs math",
        "type_": "deterministic",
        "metrics": {"checks": ["percentage_bound", "total_sum", "growth_consistency", "unit_consistency", "date_arithmetic", "table_growth"]},
        "thresholds": {"relative_tolerance": 0.02},
        "status": "active",
    },
    {
        "model_name": "multimodal-risk-fusion-v1",
        "model_type": "risk_fusion",
        "version": "1.0.0",
        "dataset": "weighted modular signals (text/claim/source/conflict/media/numerical/anomaly/propagation)",
        "type_": "weighted_fusion",
        "metrics": {"output": "risk 0-100, confidence 0-100, level"},
        "thresholds": {"medium": 0.28, "high": 0.5, "critical": 0.75},
        "status": "active",
    },
]


def main() -> int:
    registry = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {"models": []}
    models = registry.setdefault("models", [])
    existing = {(m.get("model_name"), m.get("version")) for m in models}
    added = 0
    now = datetime.now(timezone.utc).isoformat()
    for spec in VERIFICATION_MODELS:
        key = (spec["model_name"], spec["version"])
        if key in existing:
            continue
        models.append({
            "model_name": spec["model_name"],
            "version": spec["version"],
            "model_type": spec["model_type"],
            "entry_type": spec["type_"],
            "horizon_minutes": None,
            "trained_at": spec.get("trained_at") or now,
            "dataset": spec["dataset"],
            "metrics": spec["metrics"],
            "thresholds": spec.get("thresholds", {}),
            "status": spec["status"],
            "registered_at": now,
        })
        added += 1
    registry["updated_at"] = now
    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    print(f"registry updated: +{added} verification models, total {len(models)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
