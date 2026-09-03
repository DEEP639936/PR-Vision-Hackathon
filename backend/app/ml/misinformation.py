"""Misinformation-risk component (spec #16).

IMPORTANT LIMITATIONS (documented honestly, per spec #52 — no fake claims):
- The supervised model is trained on a SYNTHETIC corpus whose labels were
  generated from linguistic patterns, NOT verified fact-checks. It estimates
  *stylistic risk* of misinformation-like content.
- The system therefore NEVER claims "this post IS misinformation"; it reports
  an estimated risk with a label, always phrased as "high estimated
  misinformation risk" in user-facing copy.

Architecture:
    risk = 0.65 * TF-IDF+LogReg probability   (when the model artifact exists)
         + 0.35 * transparent lexicon heuristic (sensational/claim/urgency)
    risk = heuristic alone when no model is trained yet (clearly labelled).

Labels:  0.00-0.30 LOW · 0.30-0.60 MODERATE · 0.60-0.80 HIGH · 0.80-1.00 CRITICAL
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import joblib
    HAS_ML_RUNTIME = True
except ImportError:  # light publish runtime — see app/ml/runtime.py
    HAS_ML_RUNTIME = False

from app.core.logging import get_logger
from app.ml.feature_engineering import nlp_features
from app.ml.runtime import ML_RUNTIME

logger = get_logger("prvision.ml.misinfo")

MODEL_NAME = "prvision-misinformation-risk"

RISK_LABELS = [
    (0.30, "LOW"),
    (0.60, "MODERATE"),
    (0.80, "HIGH"),
    (1.01, "CRITICAL"),
]


def risk_label(score: float) -> str:
    for threshold, label in RISK_LABELS:
        if score < threshold:
            return label
    return "CRITICAL"


# ------------------------------------------------------------- heuristic layer
def heuristic_risk(content: str) -> float:
    """Transparent lexicon-based risk in [0,1] (documented, explainable)."""
    f = nlp_features(content)
    score = (
        0.34 * f["claim_score"]
        + 0.30 * f["sensational_score"]
        + 0.22 * f["urgency_score"]
        + 0.08 * min(1.0, f["exclamation_count"] / 8.0)
        + 0.06 * min(1.0, f["capital_ratio"] * 3.0)
    )
    # Positive, non-urgent language slightly lowers risk.
    if f["sentiment_score"] > 0.5 and f["urgency_score"] < 0.2:
        score -= 0.05
    return round(min(1.0, max(0.0, score)), 4)


# ----------------------------------------------------------------- corpus
def build_synthetic_corpus() -> tuple[list[str], list[int]]:
    """Labeled synthetic corpus for the stylistic risk model.

    Labels are derived from template categories (1 = misinfo-styled, 0 = benign).
    This is documented as synthetic — it teaches the model the *style*, and the
    heuristic layer + explainability keep the system honest.
    """
    benign_seeds = [
        "Community volunteers planted 300 trees along the river trail this weekend.",
        "The bakery on 5th avenue now opens at 7am — their sourdough sells out fast.",
        "Match report: late equaliser keeps us second in the table with three games left.",
        "Registration for the city marathon opens Monday; routes published on the council site.",
        "Our new library wing opens next month with study rooms and a coffee bar.",
        "A study from the university found regular walking improves sleep quality.",
        "Weather service forecasts light rain tomorrow morning, clearing by noon.",
        "Local school wins regional robotics championship after months of practice.",
        "The museum's new dinosaur exhibit opens Friday with free entry for kids.",
        "Reminder: council votes on the cycling plan in next week's public meeting.",
        "Photos from Saturday's charity bake sale — we raised £2,400 for the shelter.",
        "The ferry to the islands resumes its summer schedule from next Tuesday.",
        "Scientists publish new map of coral reefs off the northern coast.",
        "Job fair at the civic centre on Thursday features 60 local employers.",
        "The ducklings at the pond are out and about — adorable morning walk photos.",
        "Train services run normally this weekend despite platform repairs downtown.",
    ]
    misinfo_seeds = [
        "BREAKING!!! Doctors DON'T want you to know this miracle cure destroys viruses instantly — share before DELETED!!!",
        "EXPOSED: leaked document PROVES chemicals in the water are mind-control — mainstream media blackout!!!",
        "URGENT WARNING: 5G towers near schools making children sick — nurses being ERASED from the internet!!!",
        "SHOCKING truth: banks FURIOUS as this one weird trick pays your mortgage in 30 days!!!",
        "THEY DON'T WANT YOU TO KNOW — celebrity secretly arrested, media blackout confirmed by insiders!!!",
        "BANNED in 43 countries: supermarkets secretly spraying vegetables with toxic chemical — whistleblower speaks!!!",
        "100% PROVEN miracle remedy cures diabetes in one week — doctors hate this secret!!!",
        "ALERT!!! Government planting surveillance chips in new bank cards — destroy them NOW before it's too late!!!",
        "Leaked files reveal the REAL cause of power outages — conspiracy at the highest level, share everywhere!!!",
        "CURE for cancer suppressed for 40 years — courageous insider finally tells the truth!!!",
        "URGENT: vaccines contain secret trackers — nurses confirm, media refuses to report!!!",
        "BOMBSHELL: election machines HACKED, leaked footage PROVES fraud — share before deleted!!!",
        "They are hiding this SHOCKING secret: fluoride in water lowers IQ, documents prove!!!",
        "MIRACLE: this common kitchen spice melts fat overnight — nutritionists FURIOUS it leaked!!!",
        "EXPOSED!!! Secret club of elites controls the weather — leaked evidence inside, spread the word!!!",
        "WARNING: new currency law lets banks seize savings TONIGHT — withdraw everything NOW!!!",
    ]
    # Expand with light noise for a slightly larger corpus.
    texts, labels = [], []
    variants = ["", " SHARE THIS EVERYWHERE!!! ", " (a friend sent this) ", " RT if you agree ", ""]
    for t in misinfo_seeds:
        for v in variants[:3]:
            texts.append(t + v)
            labels.append(1)
    for t in benign_seeds:
        for v in variants[:3]:
            texts.append(t + v)
            labels.append(0)
    return texts, labels


# ----------------------------------------------------------------- wrapper
class MisinformationModel:
    """TF-IDF (1-2 grams) + LogisticRegression stylistic-risk model."""

    def __init__(self, version: str, vectorizer: Any, classifier: Any) -> None:
        self.version = version
        self.vectorizer = vectorizer
        self.classifier = classifier

    def probability(self, content: str) -> float:
        x = self.vectorizer.transform([content or ""])
        proba = self.classifier.predict_proba(x)[0]
        # identify the "risky" class index robustly
        classes = list(getattr(self.classifier, "classes_", [0, 1]))
        idx = classes.index(1) if 1 in classes else len(classes) - 1
        return float(proba[idx])

    def save(self, models_dir: Path) -> Path:
        models_dir.mkdir(parents=True, exist_ok=True)
        path = models_dir / f"misinformation__{self.version}.joblib"
        joblib.dump({"vectorizer": self.vectorizer, "classifier": self.classifier,
                     "version": self.version}, path)
        return path

    @classmethod
    def load(cls, models_dir: Path, version: str) -> "MisinformationModel":
        if not HAS_ML_RUNTIME:
            # The pickled TF-IDF/LogReg pipeline needs scikit-learn to unpickle.
            raise RuntimeError(ML_RUNTIME["reason"])
        path = models_dir / f"misinformation__{version}.joblib"
        if not path.exists():
            raise FileNotFoundError(f"Misinfo artifact not found: {path}")
        payload = joblib.load(path)
        return cls(version=payload["version"], vectorizer=payload["vectorizer"],
                   classifier=payload["classifier"])


def blend_risk(model_proba: float | None, heuristic: float) -> tuple[float, str]:
    """Combine model + heuristic; report which layer produced the score."""
    if model_proba is None:
        return round(heuristic, 4), "heuristic"
    blended = 0.65 * model_proba + 0.35 * heuristic
    return round(min(1.0, max(0.0, blended)), 4), "model+heuristic"
