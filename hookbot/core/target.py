"""Target audience: from the channel audit to other content made for the same people.

1. ``channel_signals`` — what the data says about who watches: follower and
   engaged-audience demographics, viewers' comments, the hooks that held and
   lost them, the audit findings.
2. The model turns the signals into an audience profile and searches for
   creators serving the same audience; every candidate is verified through
   the Instagram API before it is followed.
3. Their reels are stored under the audience tag, labeled within creator
   (viral vs. typical) and studied blind with the same rubric as everything.
4. ``gap_report`` — what works for this audience in other creators' reels,
   how the creator's own reels differ, and whether the creator's own audit
   and the literature agree. Recommendations need all of: an effect in the
   audience corpus, the creator lagging on it, and no contradiction from the
   creator's own audit.
"""

from __future__ import annotations

from typing import Any

from . import audience, corpus, stats
from .features import FEATURE_LITERATURE
from .corpus import CONTROL_LABELS, MIN_GROUP, Corpus
from .memory import MemoryStore, now_iso

TAG_PREFIX = "audience:"
PROFILE_WS = "__audience_profile__:"
GAP_WS = "__audience_gap__:"


def tag(workspace: str) -> str:
    """Niche tag under which content for this creator's audience is collected."""
    return TAG_PREFIX + workspace


def _brief(r: dict[str, Any]) -> dict[str, Any]:
    a = r["analysis"] or {}
    return {
        "hook": a.get("spoken_hook") or a.get("on_screen_text") or "",
        "caption": (r["caption"] or "")[:300],
        "format": a.get("format"),
        "triggers": a.get("trigger_ids", []),
        "skip_rate": r["skip_rate"],
        "views": r["views"],
        "shares": r["shares"],
        "saves": r["saves"],
    }


def channel_signals(
    store: MemoryStore,
    workspace: str,
    *,
    demographics: dict[str, Any] | None = None,
    comments: list[str] | None = None,
) -> dict[str, Any]:
    profile = store.get_profile(workspace)
    rows = audience.own_reels(store, workspace)
    studied = [r for r in rows if r["status"] == "studied"]
    by_hold = sorted((r for r in studied if r["skip_rate"] is not None), key=lambda r: r["skip_rate"])
    by_views = sorted((r for r in studied if r["views"]), key=lambda r: -r["views"])
    report = audience.latest_report(store, workspace)
    findings = [
        {"variable": e["label"], "rho": e["rho"], "q": e["q"], "verdict": e["verdict"]}
        for e in (report or {}).get("hold_effects", []) if e["verdict"] != "не подтверждено"
    ][:8]
    return {
        "niche": profile["niche"],
        "stated_audience": profile["audience"],
        "n_reels": len(rows),
        "demographics": demographics or {},
        "comments_sample": (comments or [])[:60],
        "best_by_hold": [_brief(r) for r in by_hold[:6]],
        "worst_by_hold": [_brief(r) for r in by_hold[::-1][:4]] if len(by_hold) >= 8 else [],
        "top_by_views": [_brief(r) for r in by_views[:6]],
        "audit_findings": findings,
    }


def save_profile(store: MemoryStore, workspace: str, profile: dict[str, Any], creators: list[dict[str, Any]]) -> None:
    store.save_insight(PROFILE_WS + workspace, {"profile": profile, "creators": creators, "at": now_iso()},
                       n_results=len(creators))


def latest_profile(store: MemoryStore, workspace: str) -> dict[str, Any] | None:
    insight = store.latest_insight(PROFILE_WS + workspace)
    return insight["content"] if insight else None


def gap_report(store: MemoryStore, workspace: str) -> dict[str, Any]:
    rows = Corpus(store).studied(tag(workspace))
    viral = [r for r in rows if r["label"] == "viral"]
    control = [r for r in rows if r["label"] in CONTROL_LABELS]
    own = [r for r in audience.own_reels(store, workspace) if r["status"] == "studied"]

    works = corpus.feature_effects(rows)  # what separates viral from typical reels for this audience
    by_item = {s["id"]: s for s in corpus.evidence_synthesis(works)}

    def literature_of(variable: str) -> dict[str, Any] | None:
        kind, _, name = variable.partition(":")
        item = name if kind == "trigger" else FEATURE_LITERATURE.get(name, (None, None))[1] if kind == "feature" else None
        return by_item.get(item) if item else None

    gaps = []
    for key, label, kind, _, get in corpus._variables(viral + own):
        a = [float(v) for v in (get(r) for r in viral) if v is not None]
        b = [float(v) for v in (get(r) for r in own) if v is not None]
        if len(a) < MIN_GROUP or len(b) < MIN_GROUP or (kind == "binary" and not any(a + b)):
            continue
        _, p = stats.mann_whitney(a, b)
        scale = 100.0 if kind == "binary" else 1.0
        gaps.append({
            "variable": key, "label": label, "kind": kind,
            "audience_viral": round(scale * (stats.mean(a) if kind == "binary" else stats.median(a)), 1),
            "own": round(scale * (stats.mean(b) if kind == "binary" else stats.median(b)), 1),
            "diff": round(scale * (stats.mean(a) - stats.mean(b)), 1),
            "p": p,
        })
    for g, q in zip(gaps, stats.benjamini_hochberg([g["p"] for g in gaps]) if gaps else []):
        g["p"], g["q"] = round(g["p"], 4), round(q, 4)
        g["verdict"] = "разрыв подтверждён" if q < 0.10 else ("тенденция" if q < 0.25 else "нет разрыва")
    gaps.sort(key=lambda g: (g["q"], -abs(g["diff"])))
    by_gap = {g["variable"]: g for g in gaps}

    own_audit = {e["variable"]: e for e in (audience.latest_report(store, workspace) or {}).get("hold_effects", [])}
    recommendations = []
    for e in works:
        if not (e["verdict"].startswith("подтверждено") or e["verdict"].startswith("тенденция")):
            continue
        up = e["diff"] > 0
        g = by_gap.get(e["variable"])
        if g is None or (g["audience_viral"] <= g["own"] if up else g["audience_viral"] >= g["own"]):
            continue  # the creator already does it at least as much as the winners
        mine = own_audit.get(e["variable"])
        if mine and mine["verdict"] != "не подтверждено" and (mine["rho"] > 0) != up:
            agreement = "у вашей аудитории наоборот — не применять вслепую, проверить A/B"
        elif mine and mine["verdict"] != "не подтверждено":
            agreement = f"подтверждается и в вашем канале (ρ={mine['rho']:+.2f})"
        else:
            agreement = "в вашем канале пока не проверено"
        lit = literature_of(e["variable"])
        recommendations.append({
            "variable": e["variable"], "label": e["label"], "do": "больше" if up else "меньше",
            "audience_viral": g["audience_viral"], "audience_typical": e["control"],
            "own": g["own"], "kind": e["kind"], "audience_q": e["q"], "gap_q": g["q"],
            "own_audit": agreement, "literature": (f"{lit['status']} [литература {lit['literature_level']}]" if lit else ""),
        })

    exemplars = []
    for r in sorted(viral, key=lambda r: -(r["outlier_score"] or 0))[:5]:
        a = r["analysis"] or {}
        exemplars.append({"creator": r["creator"], "url": r["url"], "outlier_score": r["outlier_score"],
                          "hook": a.get("spoken_hook") or a.get("on_screen_text") or "", "visual": a.get("visual", ""),
                          "triggers": a.get("trigger_ids", []), "hypothesis": a.get("hypothesis", "")})
    return {
        "workspace": workspace,
        "generated_at": now_iso(),
        "n_creators": len({r["creator"] for r in rows}),
        "n_viral": len(viral),
        "n_typical": len(control),
        "n_own": len(own),
        "works": works,
        "gaps": gaps,
        "recommendations": recommendations,
        "exemplars": exemplars,
    }


def save_gap(store: MemoryStore, report: dict[str, Any]) -> None:
    store.save_insight(GAP_WS + report["workspace"], report, n_results=report["n_viral"] + report["n_typical"])


def latest_gap(store: MemoryStore, workspace: str) -> dict[str, Any] | None:
    insight = store.latest_insight(GAP_WS + workspace)
    return insight["content"] if insight else None


def prompt_block(gap: dict[str, Any] | None, limit: int = 5) -> str:
    if not gap or not gap["recommendations"]:
        return ""
    lines = [f"### Контент для этой же аудитории у других авторов ({gap['n_creators']} авторов, "
             f"{gap['n_viral']} залетевших vs {gap['n_typical']} обычных)"]
    for r in gap["recommendations"][:limit]:
        unit = "%" if r["kind"] == "binary" else ""
        lines.append(f"- {r['do'].capitalize()}: {r['label']} — у залетевших {r['audience_viral']}{unit}, "
                     f"у автора {r['own']}{unit}; {r['own_audit']}")
    for ex in gap["exemplars"][:3]:
        lines.append(f"- Пример залетевшего хука ({ex['outlier_score']:.1f}× медианы автора): «{ex['hook']}»")
    return "\n".join(lines)
