"""Portable exported-weights inference engine (no numeric ML stack required).

WHY THIS EXISTS
---------------
The one-click platform publish runtime is intentionally light (no numpy /
scikit-learn / xgboost — they made the publish build stall, see
app/ml/runtime.py). That left published instances unable to serve the REAL
trained models, falling back to the velocity baseline with a "models not
trained yet" banner even though trained artifacts were bundled on disk.

This engine closes that gap HONESTLY: after training (on any full-runtime
host — sandbox, Docker, VPS), ``scripts/export_portable_models.py`` exports
the *actual learned parameters* of every trained model into a single JSON
bundle:

    forecast models  -> XGBoost booster dump (tree structure + float32 leaf
                        weights + base score, objective reg:squarederror)
    misinfo model    -> TF-IDF vocabulary + learned IDF weights +
                        LogisticRegression coefficients & intercept

… and this module replays EXACTLY those parameters in pure Python. It is the
same trained model — not a simulation, not a re-training, and not fabricated
output. Numerical parity against the native stack is enforced by
``scripts/validate_portable_parity.py`` (forecast tolerance accounts for the
float32 precision of XGBoost's dumped leaf weights; the TF-IDF/LogReg path is
float64-exact to ~1e-12).

On hosts that DO have the full runtime, the native loaders are used and this
engine is ignored.
"""
from __future__ import annotations

import json
import math
import re
import struct
from pathlib import Path
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger("prvision.ml.portable")

PORTABLE_DIRNAME = "portable"
PORTABLE_FILENAME = "portable_models.json"
FORMAT_VERSION = 1


def _f32(v: float) -> float:
    """Round to IEEE-754 single precision — replicates XGBoost's internal
    bst_float semantics. Split routing happens in float32 inside libxgboost,
    so near-boundary feature values must be compared in float32 too."""
    try:
        return struct.unpack("f", struct.pack("f", v))[0]
    except (OverflowError, ValueError):
        return v  # +-inf / nan pass through


class PortableLoadError(Exception):
    """Raised when the portable bundle is missing or unreadable."""


# --------------------------------------------------------------------- helpers
def _sigmoid(x: float) -> float:
    # Numerically stable logistic function (same as scipy.special.expit).
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def _vector(row: dict[str, Any], feature_names: list[str]) -> list[float]:
    """Same None->0.0 discipline as ForecastModel._vector, in pure Python."""
    return [0.0 if row.get(name) is None else float(row.get(name)) for name in feature_names]


# --------------------------------------------------------------- forecast trees
def _tree_leaf_sum(node: dict[str, Any], x: list[float]) -> float:
    """Traverse one XGBoost dumped-tree node; return its leaf value.

    XGBoost dump format (get_dump(dump_format="json")):
      split node: {"nodeid", "split" ("f12" or a feature name), "split_condition",
                   "yes", "no", "missing", "children", ...}
      leaf node : {"nodeid", "leaf"}
    The split test is `value < split_condition` -> yes branch, else no branch;
    NaN/None features follow the "missing" branch.
    """
    if "leaf" in node:
        return float(node["leaf"])
    split = str(node.get("split") or "")
    idx = int(split[1:]) if split.startswith("f") and split[1:].isdigit() else None
    value = x[idx] if idx is not None and 0 <= idx < len(x) else _resolve_named(x, split)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        nxt = node["missing"]
    elif _f32(value) < _f32(float(node["split_condition"])):
        nxt = node["yes"]
    else:
        nxt = node["no"]
    for child in node.get("children", []):
        if child.get("nodeid") == nxt:
            return _tree_leaf_sum(child, x)
    # Should never happen with a well-formed dump — fail loudly, honestly.
    raise PortableLoadError(f"Malformed tree dump: node {node.get('nodeid')} -> missing child {nxt}")


_NAMED_CACHE: dict[str, Optional[int]] = {}


def _resolve_named(x: list[float], name: str) -> Optional[float]:
    """Fallback when the booster dumped real feature names instead of f-indices.

    Named dumps are only used when feature_names were provided at train time;
    the export script rewrites splits to f-indices, so this path is a safety
    net that keeps evaluation deterministic (returns None -> missing branch).
    """
    return None


class PortableForecastModel:
    """Replays an exported XGBRegressor (objective reg:squarederror).

    prediction = base_score + sum of leaf values over the FIRST n_trees_used
    trees (early stopping discards the tail — XGBRegressor.predict uses
    iteration_range=(0, best_iteration+1), and we mirror that exactly).
    Split routing compares in float32, like the native kernel.
    """

    def __init__(self, horizon_minutes: int, version: str,
                 feature_names: list[str], base_score: float,
                 trees: list[dict], n_trees_used: Optional[int] = None) -> None:
        self.horizon_minutes = horizon_minutes
        self.version = version
        self.feature_names = feature_names
        self.base_score = base_score
        self.trees = trees
        self.n_trees_used = int(n_trees_used) if n_trees_used else len(trees)
        self.engine = "portable-weights"

    def predict(self, feature_row: dict[str, Any]) -> float:
        x = _vector(feature_row, self.feature_names)
        total = self.base_score
        for tree in self.trees[: self.n_trees_used]:
            total += _tree_leaf_sum(tree, x)
        return max(0.0, total)


# ----------------------------------------------------------- misinformation
class PortableMisinformationModel:
    """Replays the exported TF-IDF(1-2 grams, sublinear, l2) + LogReg pipeline.

    Mirrors scikit-learn's CountVectorizer/TfidfTransformer semantics with the
    vectorizer's own exported parameters (token_pattern, vocabulary_, idf_),
    then LogisticRegression via the stable sigmoid of the decision function.
    """

    def __init__(self, version: str, token_pattern: str, lowercase: bool,
                 ngram_range: tuple[int, int], sublinear_tf: bool,
                 vocabulary: dict[str, int], idf: list[float],
                 coef: list[float], intercept: float, classes: list[int]) -> None:
        self.version = version
        self.token_re = re.compile(token_pattern)
        self.lowercase = lowercase
        self.ngram_range = ngram_range
        self.sublinear_tf = sublinear_tf
        self.vocabulary = vocabulary
        self.idf = idf
        self.coef = coef
        self.intercept = intercept
        self.classes = classes
        self.engine = "portable-weights"
        self._norm_coef_sq = math.sqrt(sum(c * c for c in coef)) or 1.0

    def _analyzer(self, text: str) -> list[str]:
        if self.lowercase:
            text = text.lower()
        tokens = self.token_re.findall(text)
        lo, hi = self.ngram_range
        out: list[str] = []
        for n in range(lo, hi + 1):
            if n == 1:
                out.extend(tokens)
            else:
                out.extend(" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1))
        return out

    def probability(self, content: str) -> float:
        counts: dict[int, float] = {}
        for gram in self._analyzer(content or ""):
            idx = self.vocabulary.get(gram)
            if idx is not None:
                counts[idx] = counts.get(idx, 0.0) + 1.0
        if not counts:
            # All-zero vector: decision = intercept only (matches sklearn).
            d = self.intercept
            return _sigmoid(d) if (1 in self.classes) else 1.0 - _sigmoid(d)
        if self.sublinear_tf:
            vec = {i: (1.0 + math.log(c)) * self.idf[i] for i, c in counts.items()}
        else:
            vec = {i: c * self.idf[i] for i, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        decision = self.intercept
        for i, v in vec.items():
            decision += (v / norm) * self.coef[i]
        prob = _sigmoid(decision)
        # classes_ is [0, 1] for this model; "risky" class index resolved
        # exactly like MisinformationModel.probability does natively.
        risky = self.classes.index(1) if 1 in self.classes else len(self.classes) - 1
        return prob if risky == 1 else 1.0 - prob


# ------------------------------------------------------------------ bundle I/O
class PortableBundle:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.exported_at = payload.get("exported_at")
        self.source_versions: dict[str, str] = payload.get("source_versions", {})
        self.forecast: dict[int, PortableForecastModel] = {}
        for key, m in (payload.get("forecast") or {}).items():
            self.forecast[int(key)] = PortableForecastModel(
                horizon_minutes=int(m["horizon_minutes"]),
                version=m["version"],
                feature_names=list(m["feature_names"]),
                base_score=float(m["base_score"]),
                trees=m["trees"],
                n_trees_used=m.get("n_trees_used"),
            )
        mis = payload.get("misinformation")
        self.misinfo: Optional[PortableMisinformationModel] = None
        if mis:
            self.misinfo = PortableMisinformationModel(
                version=mis["version"],
                token_pattern=mis["token_pattern"],
                lowercase=bool(mis.get("lowercase", True)),
                ngram_range=tuple(mis.get("ngram_range", [1, 2])),
                sublinear_tf=bool(mis.get("sublinear_tf", True)),
                vocabulary=mis["vocabulary"],
                idf=mis["idf"],
                coef=mis["coef"],
                intercept=float(mis["intercept"]),
                classes=[int(c) for c in mis.get("classes", [0, 1])],
            )

    def ok(self) -> bool:
        return bool(self.forecast) and self.misinfo is not None


def portable_path(models_dir: Path) -> Path:
    return Path(models_dir) / PORTABLE_DIRNAME / PORTABLE_FILENAME


def load_portable(models_dir: Path) -> Optional[PortableBundle]:
    """Load the portable bundle if present; None when absent (honest cold start)."""
    path = portable_path(models_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if int(payload.get("format_version", 0)) != FORMAT_VERSION:
            logger.warning("Portable bundle format mismatch in %s — ignoring", path)
            return None
        bundle = PortableBundle(payload)
        logger.info("Portable model bundle loaded from %s (exported %s, %d forecast models)",
                    path, bundle.exported_at, len(bundle.forecast))
        return bundle
    except Exception as exc:
        logger.error("Failed to load portable bundle %s: %s", path, exc)
        return None
