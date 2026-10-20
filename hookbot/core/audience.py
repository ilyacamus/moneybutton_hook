"""Audit of the creator's own channel: what holds *this* audience.

Unit of analysis: the creator's own published reels, each with its real
3-second hold (100 − reels_skip_rate) and views, and a blind analysis of its
hook (same rubric, triggers and objective features as everywhere else).

Two complementary views:

* **Hold** — rank correlation (Spearman) of every hook variable with the
  3-second hold, p-values by permutation test, Benjamini–Hochberg q-values.
* **Reach** — the channel's breakout reels (≥3× its own median views) versus
  its typical reels, with the same effect statistics as the corpus.

Both are joined with the literature grades, so a finding reads as
"science + your audience agree", "your audience differs", or "hypothesis".
"""

from __future__ import annotations

import random
from typing import Any, Callable

from . import corpus, stats
from .corpus import CONTROL_LABELS, Corpus
from .memory import MemoryStore, now_iso

OWN_SOURCES = ("instagram_own", "own_csv", "own_file")
REPORT_WS = "__audience__:"
MIN_REELS = 8
MIN_VIEWS = 100  # rates on fewer views are noise
PERMUTATIONS = 2000


def own_reels(store: MemoryStore, workspace: str) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        f"SELECT * FROM reels WHERE workspace = ? AND source IN ({', '.join('?' * len(OWN_SOURCES))}) ORDER BY id",
        (workspace, *OWN_SOURCES),
    ).fetchall()
    return [Corpus._row(r) for r in rows]


def _permutation_p(x: list[float], y: list[float], rho: float, seed: int = 11) -> float:
    rng = random.Random(seed)
    y = list(y)
    extreme = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(y)
        r = stats.spearman(x, y)
        if r is not None and abs(r) >= abs(rho) - 1e-12:
            extreme += 1
    return (extreme + 1) / (PERMUTATIONS + 1)


def _hold(r: dict[str, Any]) -> float | None:
    return 100 - r["skip_rate"] if r["skip_rate"] is not None else None


def _views(r: dict[str, Any]) -> float | None:
    """Views relative to the channel's median of other mature reels (set by ``Corpus.label_all``)."""
    return r["outlier_score"]


def _rate(field: str) -> Callable[[dict[str, Any]], float | None]:
    def rate(r: dict[str, Any]) -> float | None:
        if r["outlier_score"] is None or not r["views"] or r["views"] < MIN_VIEWS:
            return None  # too fresh or too few views for a stable rate
        if field == "engagement":
            if r["likes"] is None:
                return None
            return 100 * (r["likes"] + (r["comments"] or 0)) / r["views"]
        return 100 * r[field] / r["views"] if r[field] is not None else None
    return rate


# Outcome → (how it is read, report key). Hold needs the private skip rate; the rest is public or from the table.
OUTCOMES = {
    "hold": (_hold, "удержание 3 с, %"),
    "views": (_views, "просмотры, × медианы канала"),
    "engagement": (_rate("engagement"), "лайки и комментарии на 100 просмотров"),
    "saves": (_rate("saves"), "сохранения на 100 просмотров"),
}


def outcome_correlations(rows: list[dict[str, Any]], outcome: str = "hold") -> list[dict[str, Any]]:
    """Spearman correlation of each hook variable with one outcome of the channel's reels."""
    get_y = OUTCOMES[outcome][0]
    studied = [r for r in rows if r["status"] == "studied" and get_y(r) is not None]
    out = []
    for key, label, kind, direction, get in corpus._variables(studied):
        pairs = [(float(v), get_y(r)) for r, v in ((r, get(r)) for r in studied) if v is not None]
        if len(pairs) < MIN_REELS:
            continue
        xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
        if len(set(xs)) < 2:
            continue
        rho = stats.spearman(xs, ys)
        if rho is None:
            continue
        item = {"variable": key, "label": label, "kind": kind, "folk_direction": direction, "n": len(pairs),
                "rho": round(rho, 3), "p": _permutation_p(xs, ys, rho)}
        if kind == "binary":
            with_v = [y for x, y in pairs if x >= 0.5]
            without = [y for x, y in pairs if x < 0.5]
            if with_v and without:
                item[f"{outcome}_with"] = round(stats.mean(with_v), 2)
                item[f"{outcome}_without"] = round(stats.mean(without), 2)
        out.append(item)
    qs = stats.benjamini_hochberg([e["p"] for e in out]) if out else []
    for e, q in zip(out, qs):
        e["p"], e["q"] = round(e["p"], 4), round(q, 4)
        e["diff"] = e["rho"]  # sign carrier for the evidence synthesis
        e["verdict"] = corpus._verdict(e)
    return sorted(out, key=lambda e: (e["q"], -abs(e["rho"])))


def hold_correlations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Spearman correlation of each hook variable with the 3-second hold."""
    return outcome_correlations(rows, "hold")


def audience_report(store: MemoryStore, workspace: str) -> dict[str, Any]:
    rows = own_reels(store, workspace)
    studied = [r for r in rows if r["status"] == "studied"]
    with_skip = [r for r in rows if r["skip_rate"] is not None]
    views = [r["views"] for r in rows if r["views"]]
    labeled = [r for r in studied if r["label"]]
    reach_effects = corpus.feature_effects(labeled)
    hold_effects = hold_correlations(rows)
    views_effects = outcome_correlations(rows, "views")
    engagement_effects = outcome_correlations(rows, "engagement")
    saves_effects = outcome_correlations(rows, "saves")
    engagement = [y for y in map(OUTCOMES["engagement"][0], rows) if y is not None]

    def brief(r: dict[str, Any]) -> dict[str, Any]:
        a = r["analysis"] or {}
        return {"reel_id": r["id"], "url": r["url"], "hook": a.get("spoken_hook") or a.get("on_screen_text") or r["caption"][:100],
                "skip_rate": r["skip_rate"], "views": r["views"], "outlier_score": r["outlier_score"]}

    ranked = sorted((r for r in studied if r["skip_rate"] is not None), key=lambda r: r["skip_rate"])
    return {
        "workspace": workspace,
        "generated_at": now_iso(),
        "n_reels": len(rows),
        "n_studied": len(studied),
        "n_with_skip_rate": len(with_skip),
        "median_views": stats.median(views) if views else None,
        "median_skip_rate": round(stats.median([r["skip_rate"] for r in with_skip]), 1) if with_skip else None,
        "breakouts": sum(1 for r in labeled if r["label"] == "viral"),
        "typical": sum(1 for r in labeled if r["label"] in CONTROL_LABELS),
        "best_hold": [brief(r) for r in ranked[:3]],
        "worst_hold": [brief(r) for r in ranked[::-1][:3]] if len(ranked) >= 6 else [],
        "hold_effects": hold_effects,
        "hold_synthesis": corpus.evidence_synthesis(hold_effects),
        "reach_effects": reach_effects,
        "reach_synthesis": corpus.evidence_synthesis(reach_effects),
        # Public numbers: work without the skip rate (e.g. reels collected through Apify).
        "views_effects": views_effects,
        "views_synthesis": corpus.evidence_synthesis(views_effects),
        "engagement_effects": engagement_effects,
        "saves_effects": saves_effects,
        "median_engagement": round(stats.median(engagement), 2) if engagement else None,
        "min_reels": MIN_REELS,
    }


def save_report(store: MemoryStore, report: dict[str, Any]) -> None:
    store.save_insight(REPORT_WS + report["workspace"], report, n_results=report["n_studied"])


def latest_report(store: MemoryStore, workspace: str) -> dict[str, Any] | None:
    insight = store.latest_insight(REPORT_WS + workspace)
    return insight["content"] if insight else None


def prompt_block(report: dict[str, Any] | None, limit: int = 6) -> str:
    """Findings about this audience for the evaluator's prompt."""
    if not report:
        return ""
    skip = f", медианный skip rate {report['median_skip_rate']}%" if report.get("median_skip_rate") is not None else ""
    lines = [f"### Аудит канала ({report['n_studied']} своих роликов{skip})"]
    shown = 0
    for e in report["hold_effects"]:
        if e["verdict"] == "не подтверждено" or shown >= limit:
            continue
        sign = "выше" if e["rho"] > 0 else "ниже"
        lines.append(f"- {e['label']}: удержание 3 с {sign} (ρ={e['rho']:+.2f}, q={e['q']}, n={e['n']}) — {e['verdict']}")
        shown += 1
    for outcome, what in (("views", "просмотры"), ("engagement", "лайки и комментарии на просмотр"),
                          ("saves", "сохранения на просмотр")):
        for e in report.get(f"{outcome}_effects", []):
            if e["verdict"] == "не подтверждено" or shown >= limit * 2:
                continue
            sign = "выше" if e["rho"] > 0 else "ниже"
            lines.append(f"- {e['label']}: {what} {sign} (ρ={e['rho']:+.2f}, q={e['q']}, n={e['n']}) — {e['verdict']}")
            shown += 1
    for e in report["reach_effects"]:
        if e["verdict"] == "не подтверждено" or shown >= limit * 2:
            continue
        lines.append(f"- {e['label']}: у прорывных роликов канала {e['viral']} против {e['control']} у обычных — {e['verdict']}")
        shown += 1
    if shown == 0:
        lines.append(f"- Значимых закономерностей пока нет (нужно ≥{MIN_REELS} роликов; чем больше, тем точнее).")
    return "\n".join(lines)


def sync_own_results(store: MemoryStore) -> int:
    """Own studied reels with a skip rate become hooks with results (training data).

    Idempotent: the created hook id is remembered in the reel's analysis.
    """
    from . import learning  # local import: learning imports this module

    c = Corpus(store)
    rows = store.conn.execute(
        f"SELECT id FROM reels WHERE source IN ({', '.join('?' * len(OWN_SOURCES))}) "
        "AND status = 'studied' AND skip_rate IS NOT NULL",
        OWN_SOURCES,
    ).fetchall()
    n = 0
    for (reel_id,) in rows:
        reel = c.get(reel_id)
        data = reel["analysis"]
        if not reel["workspace"] or data.get("hook_id"):
            continue
        text = data.get("spoken_hook") or data.get("on_screen_text") or reel["caption"][:120] or f"Reel {reel['external_id']}"
        analysis = learning.analyze(store, reel["workspace"], data["criteria"])
        hook_id = store.add_hook(
            reel["workspace"], text, source="own_reel", on_screen_text=data.get("on_screen_text", ""),
            visual=data.get("visual", ""), niche=reel["niche"], triggers=data.get("trigger_ids", []),
            criteria=data["criteria"], score=analysis["score"], meta={"reel_id": reel_id, "url": reel["url"]},
        )
        metrics = {k: reel[k] for k in ("skip_rate", "views", "shares", "saves", "likes", "comments") if reel[k] is not None}
        store.add_result(hook_id, reel["workspace"], notes="auto: Instagram insights", **metrics)
        data["hook_id"] = hook_id
        c.save_analysis(reel_id, data, reel["features"] or {})
        n += 1
    return n
