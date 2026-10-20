"""How much should the evaluator be trusted? Three checks from the methodology:

1. **Predictive validity on accounts** — forecasts made *at evaluation time*
   versus the 3-second hold measured after publishing, compared with a naive
   baseline (the account's average so far). Only honest, out-of-sample pairs.
2. **Discriminant validity on the corpus** — does the rubric separate viral
   reels from the same creators' typical reels (AUC)?
3. **Reliability of the model as a rater** — the same hook scored several
   times independently should get the same scores (ICC(A,1), Koo & Li bands).
"""

from __future__ import annotations

from typing import Any

from . import corpus, knowledge, stats
from .memory import MemoryStore

RELIABILITY_WS = "__reliability__"


def forecast_validity(store: MemoryStore, workspace: str | None = None) -> dict[str, Any]:
    rows = store.training_rows(workspace) if workspace else store.training_rows(None)
    rows = sorted(rows, key=lambda r: r["result_at"])
    pairs = []
    history: dict[str, list[float]] = {}
    for r in rows:
        seen = history.setdefault(r["workspace"], [])
        if r["predicted_hold"] is not None and seen:
            pairs.append((r["predicted_hold"], r["hold_3s"], stats.mean(seen)))
        seen.append(r["hold_3s"])
    out: dict[str, Any] = {"n": len(pairs), "mae": None, "baseline_mae": None, "skill": None,
                           "spearman": None, "spearman_ci": None}
    if len(pairs) < 3:
        return out
    mae = stats.mean([abs(p - y) for p, y, _ in pairs])
    base = stats.mean([abs(b - y) for _, y, b in pairs])
    out.update(mae=round(mae, 1), baseline_mae=round(base, 1),
               skill=round(1 - mae / base, 3) if base > 0 else None)
    rho = stats.spearman([p for p, _, _ in pairs], [y for _, y, _ in pairs])
    if rho is not None:
        out["spearman"] = round(rho, 3)
        lo, hi = stats.bootstrap_ci(pairs, lambda s: stats.spearman([p for p, _, _ in s], [y for _, y, _ in s]), n_boot=1000)
        out["spearman_ci"] = [round(lo, 3), round(hi, 3)]
    return out


def reliability(ratings: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """``ratings[i][j]`` = criteria dict from rater run j for hook i."""
    ids = knowledge.criterion_ids()
    prior = knowledge.prior_weights()
    per_criterion = {}
    for cid in ids:
        matrix = [[float(run.get(cid, 0)) for run in hook] for hook in ratings]
        icc = stats.icc_a1(matrix)
        per_criterion[cid] = {"icc": round(icc, 3) if icc is not None else None, "band": stats.icc_band(icc)}
    totals = [[corpus.rubric_score({"criteria": run}, prior) or 0.0 for run in hook] for hook in ratings]
    icc_total = stats.icc_a1(totals)
    return {
        "n_hooks": len(ratings),
        "raters": len(ratings[0]) if ratings else 0,
        "score_icc": round(icc_total, 3) if icc_total is not None else None,
        "score_band": stats.icc_band(icc_total),
        "criteria": per_criterion,
    }


def save_reliability(store: MemoryStore, result: dict[str, Any]) -> None:
    store.save_insight(RELIABILITY_WS, result, n_results=result["n_hooks"])


def report(store: MemoryStore, workspace: str | None = None) -> dict[str, Any]:
    niche = corpus.match_niche(store, store.get_profile(workspace)["niche"]) if workspace else None
    corpus_report = corpus.latest_report(store, niche)
    latest_rel = store.latest_insight(RELIABILITY_WS)
    return {
        "forecast_account": forecast_validity(store, workspace) if workspace else None,
        "forecast_all": forecast_validity(store, None),
        "corpus": corpus_report["validity"] if corpus_report else None,
        "corpus_niche": corpus_report["niche"] if corpus_report else None,
        "reliability": latest_rel["content"] if latest_rel else None,
    }
