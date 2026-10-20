"""Reference corpus: published reels, which of them went viral, and why.

The design follows docs/METHODOLOGY.md:

* **Within-creator labels.** A reel is "viral" when its views are ≥3× the
  median of the same creator's other reels. Comparing a creator with
  themselves controls for audience size, niche and production level.
* **Maturity.** Views are only compared after ``MATURE_HOURS``, once early
  growth has played out (Szabo & Huberman, 2010).
* **Matched controls.** Every viral reel that gets studied is paired with a
  typical reel by the same creator, so the corpus never learns only from
  winners (survivorship bias, Mangel & Samaniego, 1984).
* **Blind analysis.** The model scoring a reel never sees its views or label.
* **Multiple testing.** Many hypotheses are tested at once, so p-values are
  adjusted with Benjamini–Hochberg (q-values).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from . import knowledge, stats
from .features import FEATURE_LITERATURE, FEATURE_SPECS
from .memory import MemoryStore, now_iso

VIRAL_X = 3.0
ABOVE_X = 1.5
FLOP_X = 0.67
MATURE_HOURS = 72
MIN_CREATOR_REELS = 5
MIN_GROUP = 3
CONTROL_LABELS = ("typical", "flop")
REPORT_WS = "__corpus__:"
ALL_NICHES = "*"
WEIGHT_PRIOR_STRENGTH = 40.0  # corpus weights replace the expert prior gradually
HALF_LIFE_DAYS = 90.0  # trends change: a reel this old counts half when learning weights
TREND_WINDOW_DAYS = 30


@dataclass
class ReelRecord:
    """A published reel as delivered by a source (API, dataset export, manual)."""

    source: str
    external_id: str
    url: str = ""
    creator: str = ""
    niche: str = ""
    caption: str = ""
    published_at: str | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    skip_rate: float | None = None
    followers: int | None = None
    baseline_views: float | None = None  # the creator's typical views, when the source knows it
    video_url: str = ""
    thumbnail_url: str = ""
    local_path: str = ""
    workspace: str = ""


def label_for(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= VIRAL_X:
        return "viral"
    if score >= ABOVE_X:
        return "above"
    if score >= FLOP_X:
        return "typical"
    return "flop"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Corpus:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.conn = store.conn
        self.lock = store.lock

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #

    UPDATABLE = ("url", "creator", "caption", "published_at", "views", "likes", "comments", "shares", "saves",
                 "skip_rate", "followers", "baseline_views", "video_url", "thumbnail_url", "local_path")

    def upsert(self, rec: ReelRecord) -> int:
        """Insert a reel or refresh its metrics; analysis and status are kept."""
        now = now_iso()
        data = asdict(rec)
        with self.lock, self.conn:
            row = self.conn.execute(
                "SELECT id, niche FROM reels WHERE source = ? AND external_id = ?", (rec.source, rec.external_id)
            ).fetchone()
            if row is None:
                cols = [*data, "collected_at", "updated_at"]
                cur = self.conn.execute(
                    f"INSERT INTO reels({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    (*data.values(), now, now),
                )
                return int(cur.lastrowid)
            updates = {k: data[k] for k in self.UPDATABLE if data[k] not in (None, "")}
            if rec.niche and not row["niche"]:
                updates["niche"] = rec.niche
            assignments = ", ".join(f"{k} = ?" for k in updates)
            self.conn.execute(
                f"UPDATE reels SET {assignments + ', ' if assignments else ''}updated_at = ? WHERE id = ?",
                (*updates.values(), now, row["id"]),
            )
            return int(row["id"])

    @staticmethod
    def _row(row) -> dict[str, Any]:
        reel = dict(row)
        reel["analysis"] = json.loads(reel["analysis"]) if reel["analysis"] else None
        reel["features"] = json.loads(reel["features"]) if reel["features"] else None
        return reel

    def get(self, reel_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM reels WHERE id = ?", (reel_id,)).fetchone()
        return self._row(row) if row else None

    def find(self, source: str, external_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM reels WHERE source = ? AND external_id = ?", (source, external_id)
        ).fetchone()
        return self._row(row) if row else None

    def set_label(self, reel_id: int, label: str, outlier_score: float | None = None) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE reels SET label = ?, outlier_score = ? WHERE id = ?", (label, outlier_score, reel_id)
            )

    def label_all(self, *, mature_hours: float = MATURE_HOURS, now: datetime | None = None) -> int:
        """Compute within-creator outlier scores and labels for mature reels.

        Baseline = median views of the creator's *other* mature reels (needs
        ``MIN_CREATOR_REELS`` reels in total); a baseline supplied by the
        source is used when the creator has too few reels in the corpus.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=mature_hours)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, creator, views, published_at, baseline_views, label FROM reels WHERE views IS NOT NULL"
        ).fetchall()]
        by_creator: dict[str, list[dict]] = {}
        for r in rows:
            published = _parse_time(r["published_at"])
            r["mature"] = published is None or published <= cutoff
            by_creator.setdefault(r["creator"], []).append(r)

        changed = 0
        for creator, reels in by_creator.items():
            mature = [r for r in reels if r["mature"]]
            for r in mature:
                others = [o["views"] for o in mature if o["id"] != r["id"]]
                if creator and len(others) >= MIN_CREATOR_REELS - 1:
                    baseline = stats.median(others)
                else:
                    baseline = r["baseline_views"]
                if not baseline or baseline <= 0:
                    continue
                score = round(r["views"] / baseline, 3)
                label = label_for(score)
                with self.lock, self.conn:
                    self.conn.execute(
                        "UPDATE reels SET baseline_views = ?, outlier_score = ?, label = ? WHERE id = ?",
                        (round(baseline, 1), score, label, r["id"]),
                    )
                changed += 1
        return changed

    def queue(self, limit: int) -> list[dict[str, Any]]:
        """Next reels to study: viral ones, each followed by a matched control
        from the same creator (closest publish time), then other labeled reels."""
        new = [self._row(r) for r in self.conn.execute(
            "SELECT * FROM reels WHERE status = 'new' AND label IS NOT NULL"
        ).fetchall()]
        studied_controls = {
            r["creator"] for r in self.conn.execute(
                "SELECT creator FROM reels WHERE status = 'studied' AND label IN ('typical', 'flop')"
            ).fetchall()
        }
        picked: list[dict] = []
        taken: set[int] = set()

        def take(reel: dict) -> None:
            picked.append(reel)
            taken.add(reel["id"])

        virals = sorted((r for r in new if r["label"] == "viral"), key=lambda r: -(r["outlier_score"] or 0))
        for viral in virals:
            if len(picked) >= limit:
                break
            take(viral)
            controls = [r for r in new if r["creator"] == viral["creator"] and r["label"] in CONTROL_LABELS
                        and r["id"] not in taken]
            if controls and len(picked) < limit:
                when = _parse_time(viral["published_at"]) or datetime.now(timezone.utc)
                controls.sort(key=lambda r: abs(((_parse_time(r["published_at"]) or when) - when).total_seconds()))
                take(controls[0])
        # Creators with a studied viral reel but no studied control yet.
        for reel in sorted(new, key=lambda r: r["id"]):
            if len(picked) >= limit:
                break
            if reel["id"] not in taken and reel["label"] in CONTROL_LABELS and reel["creator"] not in studied_controls:
                take(reel)
        return picked[:limit]

    def save_analysis(self, reel_id: int, analysis: dict[str, Any], features: dict[str, float]) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE reels SET status = 'studied', analysis = ?, features = ?, error = NULL, updated_at = ? WHERE id = ?",
                (json.dumps(analysis, ensure_ascii=False), json.dumps(features, ensure_ascii=False), now_iso(), reel_id),
            )

    def mark_failed(self, reel_id: int, error: str) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE reels SET status = 'failed', error = ?, updated_at = ? WHERE id = ?", (error[:500], now_iso(), reel_id)
            )

    def studied(self, niche: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reels WHERE status = 'studied' AND label IS NOT NULL"
        params: list[Any] = []
        if niche and niche != ALL_NICHES:
            sql += " AND niche = ?"
            params.append(niche)
        # Stable order: reports (bootstrap intervals) must be reproducible run to run.
        return [self._row(r) for r in self.conn.execute(sql + " ORDER BY id", params).fetchall()]

    def niches(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT niche FROM reels WHERE niche != '' AND status = 'studied'"
        ).fetchall()]

    def counts(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        labels = dict(self.conn.execute(
            "SELECT COALESCE(label, 'unlabeled'), COUNT(*) FROM reels GROUP BY label"
        ).fetchall())
        studied = dict(self.conn.execute(
            "SELECT COALESCE(label, 'unlabeled'), COUNT(*) FROM reels WHERE status = 'studied' GROUP BY label"
        ).fetchall())
        return {
            "reels": one("SELECT COUNT(*) FROM reels"),
            "creators": one("SELECT COUNT(DISTINCT creator) FROM reels WHERE creator != ''"),
            "labels": labels,
            "studied": studied,
            "failed": one("SELECT COUNT(*) FROM reels WHERE status = 'failed'"),
        }

    # ------------------------------------------------------------------ #
    # Watchlist: accounts the scout follows
    # ------------------------------------------------------------------ #

    def watch(self, workspace: str, username: str, *, platform: str = "instagram", niche: str = "") -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO watchlist(workspace, platform, username, niche, added_at) VALUES (?, ?, ?, ?, ?)",
                (workspace, platform, username.lstrip("@").lower(), niche, now_iso()),
            )

    def unwatch(self, workspace: str, username: str, *, platform: str = "instagram") -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "DELETE FROM watchlist WHERE workspace = ? AND platform = ? AND username = ?",
                (workspace, platform, username.lstrip("@").lower()),
            )

    def watchlist(self, workspace: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM watchlist", []
        if workspace is not None:
            sql += " WHERE workspace = ?"
            params.append(workspace)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY added_at", params).fetchall()]


# --------------------------------------------------------------------------- #
# Analysis: what distinguishes viral reels from the same creators' typical ones
# --------------------------------------------------------------------------- #


def rubric_score(analysis: dict[str, Any] | None, weights: dict[str, float]) -> float | None:
    criteria = (analysis or {}).get("criteria") or {}
    present = {c: float(v) for c, v in criteria.items() if c in weights}
    total = sum(weights[c] for c in present)
    if not present or total <= 0:
        return None
    return 10 * sum(weights[c] * v for c, v in present.items()) / total


def _variables(rows: list[dict[str, Any]]) -> list[tuple[str, str, str, str | None, Callable[[dict], float | None]]]:
    """(key, label, kind, folk direction, getter) for every hypothesis tested."""
    out = []
    for name, (kind, label, direction) in FEATURE_SPECS.items():
        out.append((f"feature:{name}", label, kind, direction,
                    lambda r, n=name: (r["features"] or {}).get(n)))
    for c in knowledge.rubric():
        out.append((f"criterion:{c['id']}", f"Критерий: {c['name']}", "continuous", "higher",
                    lambda r, cid=c["id"]: ((r["analysis"] or {}).get("criteria") or {}).get(cid)))
    for t in knowledge.triggers():
        out.append((f"trigger:{t['id']}", f"Триггер: {t['name']}", "binary", "higher",
                    lambda r, tid=t["id"]: float(tid in ((r["analysis"] or {}).get("trigger_ids") or []))))
    prior = knowledge.prior_weights()
    out.append(("score", "Балл рубрики (экспертные веса)", "continuous", "higher",
                lambda r: rubric_score(r["analysis"], prior)))
    return out


def _paired(viral: list[dict], control: list[dict], get: Callable[[dict], float | None]) -> tuple[int, int, int]:
    """Per creator: mean(viral) − mean(control). Returns (pairs, wins, losses)."""
    def by_creator(rows):
        groups: dict[str, list[float]] = {}
        for r in rows:
            v = get(r)
            if v is not None and r["creator"]:
                groups.setdefault(r["creator"], []).append(float(v))
        return groups

    gv, gc = by_creator(viral), by_creator(control)
    wins = losses = pairs = 0
    for creator in gv.keys() & gc.keys():
        pairs += 1
        diff = stats.mean(gv[creator]) - stats.mean(gc[creator])
        wins += diff > 0
        losses += diff < 0
    return pairs, wins, losses


def feature_effects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    viral = [r for r in rows if r["label"] == "viral"]
    control = [r for r in rows if r["label"] in CONTROL_LABELS]
    effects = []
    for key, label, kind, direction, get in _variables(rows):
        a = [float(v) for v in (get(r) for r in viral) if v is not None]
        b = [float(v) for v in (get(r) for r in control) if v is not None]
        if len(a) < MIN_GROUP or len(b) < MIN_GROUP:
            continue
        if kind == "binary" and not any(a + b):
            continue
        _, p = stats.mann_whitney(a, b)
        lo, hi = stats.bootstrap_diff_ci(a, b, n_boot=1000)
        pairs, wins, losses = _paired(viral, control, get)
        scale = 100.0 if kind == "binary" else 1.0
        effects.append({
            "variable": key,
            "label": label,
            "kind": kind,
            "folk_direction": direction,
            "n_viral": len(a),
            "n_control": len(b),
            "viral": round(scale * (stats.mean(a) if kind == "binary" else stats.median(a)), 2),
            "control": round(scale * (stats.mean(b) if kind == "binary" else stats.median(b)), 2),
            "diff": round(scale * (stats.mean(a) - stats.mean(b)), 2),
            "ci": [round(scale * lo, 2), round(scale * hi, 2)],
            "cliffs_delta": round(stats.cliffs_delta(a, b), 3),
            "p": p,
            "pairs": pairs,
            "paired_wins": wins,
            "paired_losses": losses,
            "paired_p": stats.sign_test(wins, losses) if pairs else None,
        })
    qs = stats.benjamini_hochberg([e["p"] for e in effects]) if effects else []
    for e, q in zip(effects, qs):
        e["q"] = round(q, 4)
        e["p"] = round(e["p"], 4)
        e["verdict"] = _verdict(e)
    return sorted(effects, key=lambda e: (e["q"], -abs(e["cliffs_delta"])))


def _verdict(effect: dict[str, Any]) -> str:
    up = effect["diff"] > 0
    if effect["q"] < 0.10:
        status = "подтверждено"
    elif effect["q"] < 0.25:
        status = "тенденция"
    else:
        return "не подтверждено"
    direction = effect["folk_direction"]
    if direction and (direction == "higher") != up:
        return f"{status}, против практики"
    return status


def rubric_validity(rows: list[dict[str, Any]], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Does the rubric tell viral reels from the same creators' typical ones?"""
    weights = weights or knowledge.prior_weights()
    scored = [(rubric_score(r["analysis"], weights), r) for r in rows]
    scored = [(s, r) for s, r in scored if s is not None]
    pos = [s for s, r in scored if r["label"] == "viral"]
    neg = [s for s, r in scored if r["label"] in CONTROL_LABELS]
    out: dict[str, Any] = {"n_viral": len(pos), "n_control": len(neg), "auc": None, "auc_ci": None, "spearman": None}
    if len(pos) >= MIN_GROUP and len(neg) >= MIN_GROUP:
        labeled = [(s, r["label"] == "viral") for s, r in scored if r["label"] == "viral" or r["label"] in CONTROL_LABELS]

        def auc_of(sample):
            p = [s for s, v in sample if v]
            n = [s for s, v in sample if not v]
            if not p or not n:
                raise ValueError
            return stats.auc(p, n)

        out["auc"] = round(stats.auc(pos, neg), 3)
        lo, hi = stats.bootstrap_ci(labeled, auc_of, n_boot=1000)
        out["auc_ci"] = [round(lo, 3), round(hi, 3)]
    with_score = [(s, r["outlier_score"]) for s, r in scored if r["outlier_score"]]
    if len(with_score) >= 5:
        rho = stats.spearman([s for s, _ in with_score], [math.log(o) for _, o in with_score])
        out["spearman"] = round(rho, 3) if rho is not None else None
        out["n_spearman"] = len(with_score)
    return out


def _age_days(row: dict[str, Any], now: datetime) -> float:
    published = _parse_time(row.get("published_at")) or _parse_time(row.get("collected_at")) or now
    return max(0.0, (now - published).total_seconds() / 86400)


def fit_weights(
    rows: list[dict[str, Any]],
    min_per_group: int = 10,
    *,
    half_life_days: float = HALF_LIFE_DAYS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Rubric weights learned from viral-vs-control (L2 logistic regression),
    with recent reels weighted more (trends change), blended with the expert
    prior by effective sample size."""
    now = now or datetime.now(timezone.utc)
    ids = knowledge.criterion_ids()
    data = [(r["analysis"]["criteria"], 1 if r["label"] == "viral" else 0, stats.recency_weight(_age_days(r, now), half_life_days))
            for r in rows
            if r["analysis"] and r["analysis"].get("criteria") and (r["label"] == "viral" or r["label"] in CONTROL_LABELS)]
    n_pos = sum(y for _, y, _ in data)
    if n_pos < min_per_group or len(data) - n_pos < min_per_group:
        return None
    xs = [[float(c.get(cid, 5)) for cid in ids] for c, _, _ in data]
    means = [stats.mean([x[j] for x in xs]) for j in range(len(ids))]
    sds = [math.sqrt(stats.mean([(x[j] - means[j]) ** 2 for x in xs])) or 1.0 for j in range(len(ids))]
    zs = [[(x[j] - means[j]) / sds[j] for j in range(len(ids))] for x in xs]
    sample_weights = [w for _, _, w in data]
    _, coef = stats.logistic_regression(zs, [y for _, y, _ in data], l2=1.0, sample_weights=sample_weights)
    prior = knowledge.prior_weights()
    positive = [max(c, 0.0) for c in coef]
    effective_n = sum(sample_weights)
    alpha = effective_n / (effective_n + WEIGHT_PRIOR_STRENGTH)
    if sum(positive) > 0:
        mixed = [alpha * p / sum(positive) + (1 - alpha) * prior[cid] for p, cid in zip(positive, ids)]
    else:
        mixed = [prior[cid] for cid in ids]
    total = sum(mixed)
    return {
        "weights": {cid: round(w / total, 4) for cid, w in zip(ids, mixed)},
        "log_odds_per_sd": {cid: round(c, 3) for cid, c in zip(ids, coef)},
        "n": len(data),
        "effective_n": round(effective_n, 1),
        "half_life_days": half_life_days,
        "alpha": round(alpha, 3),
    }


def trend_report(
    rows: list[dict[str, Any]], *, window_days: float = TREND_WINDOW_DAYS, now: datetime | None = None
) -> dict[str, Any]:
    """What changed recently: effects in the last ``window_days`` vs. before,
    and which hook features are becoming more common among viral reels."""
    now = now or datetime.now(timezone.utc)
    recent = [r for r in rows if _age_days(r, now) <= window_days]
    earlier = [r for r in rows if _age_days(r, now) > window_days]
    out: dict[str, Any] = {"window_days": window_days, "n_recent": len(recent), "n_earlier": len(earlier),
                           "shifts": [], "rising": [], "fading": []}
    if not recent or not earlier:
        return out
    now_effects = {e["variable"]: e for e in feature_effects(recent)}
    before_effects = {e["variable"]: e for e in feature_effects(earlier)}
    for key in now_effects.keys() & before_effects.keys():
        a, b = now_effects[key], before_effects[key]
        change = a["cliffs_delta"] - b["cliffs_delta"]
        flipped = a["cliffs_delta"] * b["cliffs_delta"] < 0 and min(abs(a["cliffs_delta"]), abs(b["cliffs_delta"])) >= 0.1
        if flipped or abs(change) >= 0.3:
            out["shifts"].append({"variable": key, "label": a["label"], "delta_now": a["cliffs_delta"],
                                  "delta_before": b["cliffs_delta"], "change": round(change, 3), "flipped": flipped})
    out["shifts"].sort(key=lambda x: -abs(x["change"]))

    # Prevalence among viral reels only: what winners do more (or less) often now.
    viral_now = [r for r in recent if r["label"] == "viral"]
    viral_before = [r for r in earlier if r["label"] == "viral"]
    if len(viral_now) >= MIN_GROUP and len(viral_before) >= MIN_GROUP:
        for key, label, kind, _, get in _variables(rows):
            if kind != "binary":
                continue
            a = [float(v) for v in (get(r) for r in viral_now) if v is not None]
            b = [float(v) for v in (get(r) for r in viral_before) if v is not None]
            if len(a) < MIN_GROUP or len(b) < MIN_GROUP or not any(a + b):
                continue
            _, p = stats.mann_whitney(a, b)
            item = {"variable": key, "label": label, "share_now": round(100 * stats.mean(a)),
                    "share_before": round(100 * stats.mean(b)), "p": round(p, 4)}
            diff = item["share_now"] - item["share_before"]
            if abs(diff) >= 15:
                (out["rising"] if diff > 0 else out["fading"]).append(item)
    return out


SYNTHESIS = {
    ("strong", "confirmed"): "доказано: наука + данные",
    ("strong", "opposite"): "данные против науки — проверить, возможен сдвиг тренда",
    ("strong", "none"): "наука за, на данных пока не видно",
    ("weak", "confirmed"): "подтверждено данными (новое знание)",
    ("weak", "opposite"): "данные против практики",
    ("weak", "none"): "гипотеза",
}


def evidence_synthesis(effects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join each knowledge-base technique with what the corpus shows for it."""
    by_var = {e["variable"]: e for e in effects}
    items = {**{t["id"]: ("trigger", t) for t in knowledge.triggers()},
             **{a["id"]: ("anti_pattern", a) for a in knowledge.anti_patterns()}}
    # (item id) -> list of (variable key, expected direction)
    tests: dict[str, list[tuple[str, str]]] = {}
    for t in knowledge.triggers():
        tests.setdefault(t["id"], []).append((f"trigger:{t['id']}", "higher"))
    for feature, (_, item_id, direction) in FEATURE_LITERATURE.items():
        tests.setdefault(item_id, []).append((f"feature:{feature}", direction))
    out = []
    for item_id, variables in tests.items():
        kind, item = items[item_id]
        level = item["evidence"]["level"]
        strength = "strong" if level in ("A", "B") else "weak"
        best = None
        for key, direction in variables:
            effect = by_var.get(key)
            if effect is None:
                continue
            if effect["verdict"].startswith("подтверждено") or effect["verdict"].startswith("тенденция"):
                agrees = (effect["diff"] > 0) == (direction == "higher")
                result = "confirmed" if agrees else "opposite"
            else:
                result = "none"
            candidate = (result, effect)
            if best is None or (best[0] == "none" and result != "none") or (result != "none" and effect["q"] < best[1]["q"]):
                best = candidate
        if best is None:
            status = "нет данных"
            effect = None
        else:
            status = SYNTHESIS[(strength, best[0])]
            effect = best[1]
        out.append({
            "id": item_id,
            "kind": kind,
            "name": item["name"],
            "literature_level": level,
            "refs": item["evidence"]["refs"],
            "status": status,
            "variable": effect["variable"] if effect else None,
            "diff": effect["diff"] if effect else None,
            "q": effect["q"] if effect else None,
            "n": (effect["n_viral"] + effect["n_control"]) if effect else 0,
        })
    order = {"доказано: наука + данные": 0, "подтверждено данными (новое знание)": 1,
             "данные против науки — проверить, возможен сдвиг тренда": 2, "данные против практики": 3}
    return sorted(out, key=lambda x: (order.get(x["status"], 9), x["q"] if x["q"] is not None else 1))


def build_report(corpus: Corpus, niche: str = ALL_NICHES, *, now: datetime | None = None) -> dict[str, Any]:
    rows = corpus.studied(None if niche == ALL_NICHES else niche)
    return {
        "niche": niche,
        "generated_at": now_iso(),
        "counts": corpus.counts() if niche == ALL_NICHES else {
            "studied": {lbl: sum(1 for r in rows if r["label"] == lbl) for lbl in ("viral", "above", "typical", "flop")}
        },
        "validity": rubric_validity(rows),
        "effects": (effects := feature_effects(rows)),
        "synthesis": evidence_synthesis(effects),
        "trends": trend_report(rows, now=now),
        "weights": fit_weights(rows, now=now),
        "thresholds": {"viral_x": VIRAL_X, "control_below_x": ABOVE_X, "mature_hours": MATURE_HOURS},
    }


def save_report(store: MemoryStore, report: dict[str, Any]) -> None:
    n = sum((report["counts"].get("studied") or {}).values())
    store.save_insight(REPORT_WS + report["niche"], report, n_results=n)


def latest_report(store: MemoryStore, niche: str | None) -> dict[str, Any] | None:
    for key in ([niche] if niche else []) + [ALL_NICHES]:
        insight = store.latest_insight(REPORT_WS + key)
        if insight:
            return insight["content"]
    return None


def match_niche(store: MemoryStore, user_niche: str) -> str | None:
    """The corpus niche tag contained in the user's free-text niche (longest wins)."""
    text = (user_niche or "").lower()
    tags = [t for t in Corpus(store).niches() if t.lower() in text]
    return max(tags, key=len) if tags else None


def prior_weights(store: MemoryStore, user_niche: str = "") -> dict[str, float] | None:
    """Corpus-learned rubric weights to use as the account's prior, if available."""
    report = latest_report(store, match_niche(store, user_niche))
    if report and report.get("weights"):
        return report["weights"]["weights"]
    return None


def significant_findings(report: dict[str, Any] | None, limit: int = 6) -> list[dict[str, Any]]:
    if not report:
        return []
    return [e for e in report.get("effects", []) if e["verdict"] != "не подтверждено"][:limit]


def benchmark(
    store: MemoryStore,
    user_niche: str,
    *,
    features: dict[str, float] | None = None,
    criteria: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Place one hook among the corpus: viral vs typical reels of the matching niche."""
    niche = match_niche(store, user_niche)
    rows = Corpus(store).studied(niche)
    viral = [r for r in rows if r["label"] == "viral"]
    control = [r for r in rows if r["label"] in CONTROL_LABELS]
    if len(viral) < MIN_GROUP:
        return None
    out: dict[str, Any] = {"niche": niche or ALL_NICHES, "n_viral": len(viral), "n_control": len(control), "items": []}
    prior = knowledge.prior_weights()
    if criteria:
        score = rubric_score({"criteria": criteria}, prior)
        vs = [s for s in (rubric_score(r["analysis"], prior) for r in viral) if s is not None]
        cs = [s for s in (rubric_score(r["analysis"], prior) for r in control) if s is not None]
        if vs:
            out["score"] = {
                "value": round(score, 1),
                "pct_of_viral": round(stats.percentile_rank(vs, score)),
                "pct_of_control": round(stats.percentile_rank(cs, score)) if cs else None,
                "viral_median": round(stats.median(vs), 1),
                "control_median": round(stats.median(cs), 1) if cs else None,
            }
    for name, value in (features or {}).items():
        if name not in FEATURE_SPECS:
            continue
        kind, label, _ = FEATURE_SPECS[name]
        va = [r["features"][name] for r in viral if r["features"] and name in r["features"]]
        ca = [r["features"][name] for r in control if r["features"] and name in r["features"]]
        if len(va) < MIN_GROUP:
            continue
        item = {"feature": name, "label": label, "kind": kind, "value": value}
        if kind == "binary":
            item["viral_share"] = round(100 * stats.mean(va))
            item["control_share"] = round(100 * stats.mean(ca)) if ca else None
        else:
            item["viral_median"] = round(stats.median(va), 2)
            item["control_median"] = round(stats.median(ca), 2) if ca else None
            item["pct_of_viral"] = round(stats.percentile_rank(va, value))
        out["items"].append(item)
    return out


def exemplars(store: MemoryStore, user_niche: str, trigger_ids: Iterable[str] = (), k: int = 3) -> list[dict[str, Any]]:
    """Studied viral reels most similar (by triggers) to a hook, from the matching niche."""
    rows = [r for r in Corpus(store).studied(match_niche(store, user_niche)) if r["label"] == "viral" and r["analysis"]]
    wanted = set(trigger_ids)

    def similarity(r):
        have = set(r["analysis"].get("trigger_ids") or [])
        jaccard = len(wanted & have) / len(wanted | have) if wanted | have else 0
        return (jaccard, r["outlier_score"] or 0)

    out = []
    for r in sorted(rows, key=similarity, reverse=True)[:k]:
        a = r["analysis"]
        out.append({
            "reel_id": r["id"], "creator": r["creator"], "url": r["url"], "outlier_score": r["outlier_score"],
            "spoken": a.get("spoken_hook", ""), "on_screen_text": a.get("on_screen_text", ""),
            "visual": a.get("visual", ""), "trigger_ids": a.get("trigger_ids", []), "hypothesis": a.get("hypothesis", ""),
        })
    return out
