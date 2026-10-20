"""Breakout potential of a hook within a niche.

What the niche's knowledge base can say about a new hook before it is filmed:
how far above the creator's usual views reels with hooks like it went.

* **Target** — ``log10(outlier_score)``: views divided by the median views of
  the same creator's other reels. Comparing a creator with themselves removes
  audience size and production level.
* **Inputs** — the 9 rubric criteria. They are the only thing known about a
  hook written for a script (there is no video yet), so the model can score
  hooks before filming.
* **Model** — ridge regression on standardized criteria (Hoerl & Kennard, 1970).
* **Honesty** — cross-validation grouped by creator: a creator's reels are
  never in both the training and the test part, so the model is checked on
  creators it has not seen. Reported: Spearman correlation between predicted
  and actual multiples (with a creator-bootstrap interval) and the median
  actual multiple of the predicted top and bottom fifths.

When a niche's reels come only from an outlier index (every reel ≥3× its
creator's median), the model ranks hooks *among breakouts* — how big a
breakout tends to be — rather than viral vs. ordinary. ``sample`` says which.

Models are trained per niche on the corpus and stored as insights. A model
trained elsewhere can be shipped in ``data/niche_models/<niche>.json`` (only
aggregate numbers: coefficients, quantiles, validation metrics; no reels).
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

from . import knowledge, niches, stats
from .corpus import CONTROL_LABELS, Corpus
from .knowledge import DATA_DIR
from .memory import MemoryStore, now_iso

MODEL_WS = "__breakout__:"
BUNDLED_DIR = DATA_DIR / "niche_models"
MIN_REELS = 60
MIN_CREATORS = 10
FOLDS = 5
RIDGE = 50.0
SEED = 7
QUALITY = ((0.35, "сильная"), (0.2, "умеренная"), (0.1, "слабая"))


def quality(rho: float | None) -> str:
    for threshold, label in QUALITY:
        if rho is not None and rho >= threshold:
            return label
    return "нет сигнала"


def training_rows(store: MemoryStore, niche: str) -> list[dict[str, Any]]:
    """Studied reels of other creators in the niche with rubric criteria and a within-creator multiple."""
    ids = knowledge.criterion_ids()
    rows = []
    for r in Corpus(store).niche_rows(niche):
        criteria = (r["analysis"] or {}).get("criteria") or {}
        if not r["outlier_score"] or r["outlier_score"] <= 0 or not r["creator"] or not criteria:
            continue
        rows.append({"x": [float(criteria[c]) if c in criteria else None for c in ids],
                     "y": math.log10(r["outlier_score"]), "creator": r["creator"], "label": r["label"]})
    # Missing criteria get the niche mean, so a partial analysis still counts.
    for j in range(len(ids)):
        present = [r["x"][j] for r in rows if r["x"][j] is not None]
        fill = stats.mean(present) if present else 5.0
        for r in rows:
            if r["x"][j] is None:
                r["x"][j] = fill
    return rows


def fit(rows: list[dict[str, Any]], ridge: float = RIDGE) -> dict[str, Any]:
    """Closed-form ridge regression on standardized criteria."""
    ids = knowledge.criterion_ids()
    n, d = len(rows), len(ids)
    means = [sum(r["x"][j] for r in rows) / n for j in range(d)]
    sds = [max(1e-9, math.sqrt(sum((r["x"][j] - means[j]) ** 2 for r in rows) / n)) for j in range(d)]
    y_mean = sum(r["y"] for r in rows) / n
    zs = [[(r["x"][j] - means[j]) / sds[j] for j in range(d)] for r in rows]
    a = [[sum(z[i] * z[j] for z in zs) + (ridge if i == j else 0.0) for j in range(d)] for i in range(d)]
    b = [sum(z[i] * (r["y"] - y_mean) for z, r in zip(zs, rows)) for i in range(d)]
    coef = stats.solve(a, b)
    return {"criteria": ids, "means": means, "sds": sds, "y_mean": y_mean, "coef": coef, "ridge": ridge}


def _raw(model: dict[str, Any], x: list[float]) -> float:
    return model["y_mean"] + sum(c * (v - m) / s for c, v, m, s in zip(model["coef"], x, model["means"], model["sds"]))


def cross_validate(rows: list[dict[str, Any]], folds: int = FOLDS, seed: int = SEED) -> dict[str, Any]:
    """Out-of-fold predictions with creators split across folds, and how well they rank reels."""
    creators = sorted({r["creator"] for r in rows})
    random.Random(seed).shuffle(creators)
    fold_of = {c: i % folds for i, c in enumerate(creators)}
    pairs: list[tuple[float, float, str]] = []
    for k in range(folds):
        train = [r for r in rows if fold_of[r["creator"]] != k]
        test = [r for r in rows if fold_of[r["creator"]] == k]
        if not train or not test:
            continue
        model = fit(train)
        pairs += [(_raw(model, r["x"]), r["y"], r["creator"]) for r in test]
    rho = stats.spearman([p for p, _, _ in pairs], [y for _, y, _ in pairs])

    # Creator bootstrap: reels of one creator are not independent, so resample creators, not reels.
    by_creator: dict[str, list[tuple[float, float]]] = {}
    for p, y, c in pairs:
        by_creator.setdefault(c, []).append((p, y))
    groups = list(by_creator.values())

    def rho_of(sample):
        flat = [pt for g in sample for pt in g]
        return stats.spearman([p for p, _ in flat], [y for _, y in flat])

    lo, hi = stats.bootstrap_ci(groups, rho_of, n_boot=300, seed=seed)
    order = sorted(range(len(pairs)), key=lambda i: pairs[i][0])
    fifth = max(1, len(order) // 5)
    bottom = [10 ** pairs[i][1] for i in order[:fifth]]
    top = [10 ** pairs[i][1] for i in order[-fifth:]]
    return {
        "folds": folds,
        "n": len(pairs),
        "spearman": round(rho, 3) if rho is not None else None,
        "spearman_ci": [round(lo, 3), round(hi, 3)] if not math.isnan(lo) else None,
        "top_fifth_median_x": round(stats.median(top), 1),
        "bottom_fifth_median_x": round(stats.median(bottom), 1),
    }


def build(store: MemoryStore, niche: str) -> dict[str, Any] | None:
    """Train and validate the niche's model; None when the niche has too little data."""
    niche = niches.canonical(niche)
    rows = training_rows(store, niche)
    n_creators = len({r["creator"] for r in rows})
    if len(rows) < MIN_REELS or n_creators < MIN_CREATORS:
        return None
    cv = cross_validate(rows)
    model = fit(rows)
    in_sample = sorted(_raw(model, r["x"]) for r in rows)
    labels = {lbl: sum(1 for r in rows if r["label"] == lbl) for lbl in ("viral", "above", "typical", "flop")}
    controls = sum(labels[lbl] for lbl in CONTROL_LABELS)
    return {
        "niche": niche,
        "niche_name": niches.name(niche),
        "trained_at": now_iso(),
        "n": len(rows),
        "n_creators": n_creators,
        "labels": labels,
        "sample": "breakouts_only" if controls == 0 else "mixed",
        "median_x": round(10 ** stats.median([r["y"] for r in rows]), 1),
        "criteria": model["criteria"],
        "means": [round(v, 4) for v in model["means"]],
        "sds": [round(v, 4) for v in model["sds"]],
        "y_mean": round(model["y_mean"], 5),
        "coef": [round(v, 5) for v in model["coef"]],
        "ridge": model["ridge"],
        "weights": {c: round(v, 4) for c, v in zip(model["criteria"], model["coef"])},
        "quantiles": [round(stats.quantile(in_sample, q / 100), 5) for q in range(101)],
        "cv": cv,
        "quality": quality(cv["spearman"]),
    }


def save(store: MemoryStore, model: dict[str, Any]) -> None:
    store.save_insight(MODEL_WS + model["niche"], model, n_results=model["n"])


def bundled(niche: str) -> dict[str, Any] | None:
    path = BUNDLED_DIR / f"{niches.canonical(niche)}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return {**json.load(f), "bundled": True}


def latest(store: MemoryStore, niche: str) -> dict[str, Any] | None:
    """The niche's model trained on this database, else the one shipped with the package."""
    niche = niches.canonical(niche)
    insight = store.latest_insight(MODEL_WS + niche)
    return insight["content"] if insight else bundled(niche)


def available(store: MemoryStore) -> dict[str, dict[str, Any]]:
    """Every niche with a model: trained here or shipped with the package."""
    out = {}
    for niche in niches.ids():
        model = latest(store, niche)
        if model:
            out[niche] = model
    for row in store.conn.execute("SELECT DISTINCT workspace FROM insights WHERE workspace LIKE ?", (MODEL_WS + "%",)):
        niche = row[0][len(MODEL_WS):]
        out.setdefault(niche, latest(store, niche))
    return out


def predict(model: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any] | None:
    """Expected multiple of the creator's median views and the hook's percentile among the niche's reels."""
    if not model or not criteria:
        return None
    present = [c for c in model["criteria"] if c in criteria]
    if not present:
        return None
    x = [float(criteria[c]) if c in criteria else m for c, m in zip(model["criteria"], model["means"])]
    raw = _raw(model, x)
    return {
        "niche": model["niche"],
        "niche_name": model.get("niche_name") or niches.name(model["niche"]),
        "expected_x": round(10 ** raw, 1),
        "percentile": round(stats.percentile_rank(model["quantiles"], raw)),
        "median_x": model["median_x"],
        "quality": model["quality"],
        "cv_spearman": model["cv"]["spearman"],
        "n": model["n"],
        "n_creators": model["n_creators"],
        "sample": model["sample"],
    }


def potential(store: MemoryStore, niche_text: str, criteria: dict[str, Any]) -> dict[str, Any] | None:
    """Breakout potential of a hook in the niche a free-text niche resolves to (None without a model)."""
    niche = niches.canonical(niche_text or "")
    return predict(latest(store, niche), criteria) if niche else None


def top_drivers(model: dict[str, Any], k: int = 3) -> tuple[list[str], list[str]]:
    """Criteria that raise and lower the expected breakout most in this niche."""
    ranked = sorted(model["weights"].items(), key=lambda kv: kv[1], reverse=True)
    up = [c for c, w in ranked if w > 0.005][:k]
    down = [c for c, w in reversed(ranked) if w < -0.005][:k]
    return up, down
