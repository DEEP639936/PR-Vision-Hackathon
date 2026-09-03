"""Portable exported-weights engine tests (pure stdlib — runs on slim runtime too)."""
from __future__ import annotations

import json

import pytest

from app.ml import portable as P


# ----------------------------------------------------------------- fixtures
@pytest.fixture()
def tiny_bundle(tmp_path):
    """A hand-built 2-tree XGBoost-style model + tiny TF-IDF/LogReg bundle."""
    trees = [
        # tree 0: split f0 < 10 ? leaf 1.5 : leaf -0.5 ; missing -> no-branch
        {"nodeid": 0, "split": "f0", "split_condition": 10.0, "yes": 1, "no": 2,
         "missing": 2, "children": [
             {"nodeid": 1, "leaf": 1.5},
             {"nodeid": 2, "split": "f1", "split_condition": 0.5, "yes": 3, "no": 4,
              "missing": 3, "children": [
                  {"nodeid": 3, "leaf": 0.25},
                  {"nodeid": 4, "leaf": -2.0},
              ]},
         ]},
        # tree 1: constant leaf
        {"nodeid": 0, "leaf": 0.75},
    ]
    payload = {
        "format_version": 1,
        "exported_at": "test",
        "source_versions": {"forecast_30": "t1", "misinformation": "t1"},
        "forecast": {
            "30": {
                "version": "t1", "horizon_minutes": 30,
                "feature_names": ["f0", "f1"],
                "base_score": 10.0, "objective": "reg:squarederror",
                "n_trees_used": 2, "trees": trees,
            },
        },
        "misinformation": {
            "version": "t1",
            "token_pattern": r"(?u)\b\w\w+\b",
            "lowercase": True, "ngram_range": [1, 2], "sublinear_tf": True,
            "norm": "l2",
            "vocabulary": {"miracle": 0, "cure": 1, "miracle cure": 2},
            "idf": [2.0, 1.5, 3.0],
            "coef": [0.8, -0.3, 1.2],
            "intercept": -0.1,
            "classes": [0, 1],
        },
    }
    out = tmp_path / "portable" / "portable_models.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps(payload))
    return payload, out


# ------------------------------------------------------------------- tests
def test_forecast_tree_traversal(tiny_bundle):
    payload, out = tiny_bundle
    bundle = P.load_portable(out.parent.parent)
    model = bundle.forecast[30]

    # f0=5 <10 -> yes-LEAF 1.5 (f1 split never visited) -> 10 + 1.5 + 0.75 = 12.25
    assert model.predict({"f0": 5.0, "f1": 0.2}) == pytest.approx(12.25)
    # f0=50 -> no -> f1=1.0 -> no(-2.0) -> 10 - 2 + 0.75 = 8.75
    assert model.predict({"f0": 50.0, "f1": 1.0}) == pytest.approx(8.75)
    # None features follow the production _vector discipline (None -> 0.0):
    # f0=0.0 -> yes-leaf 1.5 -> 12.25
    assert model.predict({"f0": None, "f1": 0.1}) == pytest.approx(12.25)
    # true NaN takes the MISSING branch -> f1=0.1 yes(0.25) -> 11.0
    assert model.predict({"f0": float("nan"), "f1": 0.1}) == pytest.approx(11.0)
    # negative total clamps to 0 (same rule as ForecastModel.predict)
    clamp_model = P.PortableForecastModel(30, "t", ["f0"], 0.0, [
        {"nodeid": 0, "split": "f0", "split_condition": 0.0, "yes": 1, "no": 1,
         "missing": 1, "children": [{"nodeid": 1, "leaf": -3.0}]}], 1)
    assert clamp_model.predict({"f0": 5.0}) == 0.0


def test_forecast_float32_boundary_routing():
    """Split routing must round to float32 like libxgboost (regression)."""
    # float64: 0.037 < 0.0370000005 -> yes. float32: equal -> no branch.
    tree = {"nodeid": 0, "split": "f0", "split_condition": 0.0370000005,
            "yes": 1, "no": 2, "missing": 2,
            "children": [{"nodeid": 1, "leaf": 1.0}, {"nodeid": 2, "leaf": 0.0}]}
    model = P.PortableForecastModel(30, "t", ["f0"], 0.0, [tree], 1)
    # Native xgboost routes 0.037 to the NO branch (float32 equality) — mirror it.
    assert model.predict({"f0": 0.037}) == pytest.approx(0.0)
    assert model.predict({"f0": 0.0}) == pytest.approx(1.0)


def test_misinformation_probability_matches_native_formula(tiny_bundle):
    """Recompute the TF-IDF(1-2, sublinear, l2)+LogReg pipeline by hand."""
    payload, out = tiny_bundle
    bundle = P.load_portable(out.parent.parent)
    mis = bundle.misinfo

    text = "Miracle cure!"
    tokens = ["miracle", "cure"]
    counts = {"miracle": 1, "cure": 1, "miracle cure": 1}
    import math
    vec = {i: (1.0 + math.log(c)) * mis.idf[i]
           for i, c in ((mis.vocabulary[g], n) for g, n in counts.items())}
    norm = math.sqrt(sum(v * v for v in vec.values()))
    decision = mis.intercept + sum((v / norm) * mis.coef[i] for i, v in vec.items())
    expected = 1.0 / (1.0 + math.exp(-decision))

    assert mis.probability(text) == pytest.approx(expected, rel=1e-12)
    # OOV-only text -> all-zero vector: decision = intercept (exactly like
    # sklearn's expit(intercept) for a zero row)
    assert mis.probability("zzz qqq") == pytest.approx(
        1.0 / (1.0 + math.exp(-mis.intercept)), rel=1e-12)
    # empty text safe
    assert 0.0 <= mis.probability("") <= 1.0


def test_missing_bundle_returns_none(tmp_path):
    assert P.load_portable(tmp_path) is None


def test_bad_format_version_ignored(tmp_path):
    d = tmp_path / "portable"
    d.mkdir()
    (d / "portable_models.json").write_text(json.dumps({"format_version": 99}))
    assert P.load_portable(tmp_path) is None
