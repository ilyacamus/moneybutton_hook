"""Self-learning: fit the rubric to real 3-second hold rates.

The model is hierarchical (empirical Bayes, Efron & Morris, 1975):

    expert rubric  →  weights learned on the viral corpus  →  this account

1. ``Calibration`` — ridge regression (Hoerl & Kennard, 1970) from the 9 rubric
   scores to the observed 3-second hold (100 − skip rate), shrunk toward the
   prior weights (the corpus weights when the corpus is large enough, else the
   expert rubric). Recent results weigh more, because audiences and trends
   drift. It yields a predicted hold for new hooks and re-weights the score.
2. ``trigger_lift`` — which psychological triggers actually raise hold for
   this account.
3. Best/worst hook examples, the latest playbook, what the viral corpus has
   confirmed scientifically and similar viral reels, injected into prompts
   (see ``LearningContext.to_prompt``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from datetime import datetime, timezone

from . import corpus, knowledge, literature, stats
from .memory import MemoryStore

MIN_RESULTS = 5
PRIOR_STRENGTH = 8.0  # the prior is worth this many observations
MAX_CV_FOLDS = 10
ACCOUNT_HALF_LIFE_DAYS = 180.0  # an account's result this old counts half
# Hooks the user brought in themselves (vs. variants we suggested) — the ones worth asking results for.
SCORED_SOURCES = ("evaluated", "video")


def _vector(criteria: dict[str, Any], ids: Sequence[str], fill: dict[str, float]) -> list[float]:
    return [float(criteria[c]) if c in criteria else fill[c] for c in ids]


def _fit_core(
    xs: list[list[float]], ys: list[float], prior: list[float], ws: list[float] | None = None
) -> tuple[list[float], float, list[float]]:
    """Weighted ridge regression shrunk toward ``slope * prior``. Returns (means, y_mean, coef)."""
    n, d = len(xs), len(prior)
    ws = ws or [1.0] * n
    total = sum(ws)
    means = [sum(w * x[j] for w, x in zip(ws, xs)) / total for j in range(d)]
    y_mean = sum(w * y for w, y in zip(ws, ys)) / total
    xc = [[x[j] - means[j] for j in range(d)] for x in xs]
    yc = [y - y_mean for y in ys]

    # How much hold does one point of the *prior* composite score buy?
    composite = [sum(p * v for p, v in zip(prior, row)) for row in xc]
    denom = sum(w * z * z for w, z in zip(ws, composite))
    slope = max(0.0, sum(w * z * y for w, z, y in zip(ws, composite, yc)) / denom) if denom > 1e-9 else 0.0
    target = [slope * p for p in prior]

    variances = [sum(w * row[j] ** 2 for w, row in zip(ws, xc)) / total for j in range(d)]
    lam = PRIOR_STRENGTH * max(sum(variances) / d, 1e-3)

    a = [[sum(w * row[i] * row[j] for w, row in zip(ws, xc)) + (lam if i == j else 0.0) for j in range(d)]
         for i in range(d)]
    b = [sum(w * row[i] * y for w, row, y in zip(ws, xc, yc)) + lam * target[i] for i in range(d)]
    return means, y_mean, stats.solve(a, b)


def _predict(means: list[float], y_mean: float, coef: list[float], x: list[float]) -> float:
    value = y_mean + sum(c * (v - m) for c, v, m in zip(coef, x, means))
    return max(0.0, min(100.0, value))


@dataclass
class Calibration:
    scope: str  # "prior" | "workspace" | "global"
    n: int
    weights: dict[str, float]
    prior_source: str = "expert"  # "expert" | "corpus"
    means: dict[str, float] | None = None
    y_mean: float | None = None
    coef: dict[str, float] | None = None
    mae: float | None = None

    @property
    def can_predict(self) -> bool:
        return self.coef is not None

    def score(self, criteria: dict[str, Any]) -> float | None:
        """Weighted 0–100 score. Missing criteria are ignored (weights renormalized)."""
        present = {c: float(v) for c, v in criteria.items() if c in self.weights}
        total = sum(self.weights[c] for c in present)
        if not present or total <= 0:
            return None
        return round(10 * sum(self.weights[c] * v for c, v in present.items()) / total, 1)

    def predict_hold(self, criteria: dict[str, Any]) -> float | None:
        """Predicted 3-second hold in percent, or ``None`` without enough data."""
        if not self.can_predict or not criteria:
            return None
        ids = list(self.coef)
        x = _vector(criteria, ids, self.means)
        return round(
            _predict([self.means[c] for c in ids], self.y_mean, [self.coef[c] for c in ids], x), 1
        )

    def weight_shifts(self) -> list[tuple[str, float]]:
        """Criteria sorted by how much their learned weight moved away from the prior."""
        prior = knowledge.prior_weights()
        shifts = [(c, round(w - prior.get(c, 0.0), 3)) for c, w in self.weights.items()]
        return sorted(shifts, key=lambda item: abs(item[1]), reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def prior_calibration(n: int = 0, prior_weights: dict[str, float] | None = None) -> Calibration:
    return Calibration(
        scope="prior",
        n=n,
        weights=prior_weights or knowledge.prior_weights(),
        prior_source="corpus" if prior_weights else "expert",
    )


def _recency(row: dict[str, Any], now: datetime, half_life_days: float) -> float:
    stamp = row.get("result_at") or row.get("created_at")
    try:
        when = datetime.fromisoformat(stamp) if stamp else now
    except ValueError:
        when = now
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return stats.recency_weight((now - when).total_seconds() / 86400, half_life_days)


def fit_calibration(
    rows: list[dict[str, Any]],
    scope: str = "workspace",
    *,
    prior_weights: dict[str, float] | None = None,
    half_life_days: float = ACCOUNT_HALF_LIFE_DAYS,
    now: datetime | None = None,
) -> Calibration:
    """Fit the calibration on rows from ``MemoryStore.training_rows``."""
    rows = [r for r in rows if r.get("criteria") and r.get("hold_3s") is not None]
    if len(rows) < MIN_RESULTS:
        return prior_calibration(n=len(rows), prior_weights=prior_weights)

    now = now or datetime.now(timezone.utc)
    ids = knowledge.criterion_ids()
    prior_w = prior_weights or knowledge.prior_weights()
    prior = [prior_w[c] for c in ids]
    neutral = {c: 5.0 for c in ids}
    xs = [_vector(r["criteria"], ids, neutral) for r in rows]
    ys = [float(r["hold_3s"]) for r in rows]
    ws = [_recency(r, now, half_life_days) for r in rows]
    n = len(rows)

    means, y_mean, coef = _fit_core(xs, ys, prior, ws)

    # k-fold cross-validated mean absolute error (leave-one-out for small n)
    folds = min(n, MAX_CV_FOLDS)
    errors: list[float] = []
    for k in range(folds):
        train = [i for i in range(n) if i % folds != k]
        test = [i for i in range(n) if i % folds == k]
        if len(train) < 2:
            continue
        m, ym, c = _fit_core([xs[i] for i in train], [ys[i] for i in train], prior, [ws[i] for i in train])
        errors += [abs(ys[i] - _predict(m, ym, c, xs[i])) for i in test]
    mae = round(sum(errors) / len(errors), 1) if errors else None

    # Learned importance: positive coefficients, blended with the prior by data volume.
    positive = [max(c, 0.0) for c in coef]
    alpha = n / (n + PRIOR_STRENGTH)
    if sum(positive) > 0:
        learned = [p / sum(positive) for p in positive]
        mixed = [alpha * lw + (1 - alpha) * pw for lw, pw in zip(learned, prior)]
    else:
        mixed = prior
    total = sum(mixed)
    weights = {c: round(w / total, 4) for c, w in zip(ids, mixed)}

    return Calibration(
        scope=scope,
        n=n,
        weights=weights,
        prior_source="corpus" if prior_weights else "expert",
        means={c: round(v, 3) for c, v in zip(ids, means)},
        y_mean=round(y_mean, 3),
        coef={c: round(v, 4) for c, v in zip(ids, coef)},
        mae=mae,
    )


def calibrate(store: MemoryStore, workspace: str) -> Calibration:
    """Workspace model when it has enough data, else the shared global model,
    else the prior (corpus-learned weights for the niche, or the expert rubric)."""
    niche = store.get_profile(workspace)["niche"]
    prior_weights = corpus.prior_weights(store, niche)
    own = store.training_rows(workspace)
    own_scored = [r for r in own if r["criteria"]]
    if len(own_scored) >= MIN_RESULTS:
        return fit_calibration(own_scored, scope="workspace", prior_weights=prior_weights)
    shared = [r for r in store.training_rows(None, shared_only=True) if r["criteria"]]
    if len(shared) >= MIN_RESULTS:
        return fit_calibration(shared, scope="global", prior_weights=prior_weights)
    return prior_calibration(n=len(own), prior_weights=prior_weights)


def trigger_lift(rows: list[dict[str, Any]], min_n: int = 2) -> list[dict[str, Any]]:
    """Average hold of hooks with vs. without each trigger, best first."""
    names = knowledge.names("trigger")
    out = []
    for trigger in sorted({t for r in rows for t in r.get("triggers", [])}):
        with_t = [r["hold_3s"] for r in rows if trigger in r["triggers"]]
        without = [r["hold_3s"] for r in rows if trigger not in r["triggers"]]
        if len(with_t) < min_n or len(without) < min_n:
            continue
        avg_with = sum(with_t) / len(with_t)
        avg_without = sum(without) / len(without)
        out.append(
            {
                "trigger_id": trigger,
                "name": names.get(trigger, trigger),
                "n": len(with_t),
                "avg_hold_with": round(avg_with, 1),
                "avg_hold_without": round(avg_without, 1),
                "lift": round(avg_with - avg_without, 1),
            }
        )
    return sorted(out, key=lambda item: item["lift"], reverse=True)


def _example(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "text", "on_screen_text", "visual", "triggers", "hold_3s", "views", "shares", "saves")
    return {k: row.get(k) for k in keys}


def select_examples(rows: list[dict[str, Any]], top: int = 5, bottom: int = 3) -> tuple[list, list]:
    ranked = sorted(rows, key=lambda r: r["hold_3s"], reverse=True)
    if len(ranked) < 4:
        return [_example(r) for r in ranked[:top]], []
    n_best = min(top, len(ranked) // 2)
    n_worst = min(bottom, len(ranked) - n_best)
    return [_example(r) for r in ranked[:n_best]], [_example(r) for r in ranked[-n_worst:][::-1]]


@dataclass
class LearningContext:
    workspace: str
    profile: dict[str, Any]
    calibration: Calibration
    n_results: int
    lifts: list[dict[str, Any]] = field(default_factory=list)
    best: list[dict[str, Any]] = field(default_factory=list)
    worst: list[dict[str, Any]] = field(default_factory=list)
    playbook: dict[str, Any] | None = None
    pending: list[dict[str, Any]] = field(default_factory=list)
    science: list[dict[str, Any]] = field(default_factory=list)
    trends: dict[str, Any] | None = None
    corpus_niche: str | None = None
    literature: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["calibration"] = self.calibration.to_dict()
        return data

    def to_prompt(self) -> str:
        """Markdown block injected into every LLM request for this workspace."""
        cal = self.calibration
        crit_names = knowledge.names("criterion")
        lines = ["## Данные самообучения по этому аккаунту", ""]
        if self.profile.get("niche"):
            lines.append(f"- Ниша: {self.profile['niche']}")
        if self.profile.get("audience"):
            lines.append(f"- Целевая аудитория: {self.profile['audience']}")
        lines.append(f"- Ролики с известным удержанием 3 с: {self.n_results}")

        if cal.scope == "prior":
            lines.append(
                f"- Калибровка: пока используются экспертные веса (нужно ≥{MIN_RESULTS} результатов)."
            )
        else:
            source = "данные этого аккаунта" if cal.scope == "workspace" else "общая анонимная база"
            mae = f", средняя ошибка прогноза ±{cal.mae} п.п." if cal.mae is not None else ""
            lines.append(f"- Калибровка: {source}, n={cal.n}, среднее удержание {cal.y_mean:.0f}%{mae}")
            grew = [c for c, delta in cal.weight_shifts() if delta > 0.01][:3]
            fell = [c for c, delta in cal.weight_shifts() if delta < -0.01][:3]
            if grew:
                lines.append("- Критерии, которые здесь важнее, чем в среднем: " + ", ".join(crit_names[c] for c in grew))
            if fell:
                lines.append("- Критерии, которые здесь важны меньше: " + ", ".join(crit_names[c] for c in fell))

        if self.lifts:
            lines += ["", "### Как триггеры влияют на удержание 3 с"]
            for item in self.lifts[:6]:
                sign = "+" if item["lift"] >= 0 else ""
                lines.append(
                    f"- `{item['trigger_id']}` ({item['name']}): {sign}{item['lift']} п.п. "
                    f"({item['avg_hold_with']}% vs {item['avg_hold_without']}%, n={item['n']})"
                )

        def render(examples: list[dict[str, Any]]) -> list[str]:
            out = []
            for ex in examples:
                extra = f" | текст: «{ex['on_screen_text']}»" if ex.get("on_screen_text") else ""
                out.append(f"- [{ex['hold_3s']:.0f}% удержания] «{ex['text']}»{extra}")
            return out

        if self.best:
            lines += ["", "### Лучшие хуки аккаунта (ориентир стиля)", *render(self.best)]
        if self.worst:
            lines += ["", "### Худшие хуки аккаунта (чего избегать)", *render(self.worst)]

        if self.playbook:
            lines += ["", "### Плейбук, выученный на данных аккаунта"]
            if self.playbook.get("summary"):
                lines.append(self.playbook["summary"])
            for rule in self.playbook.get("do_more", []):
                lines.append(f"- ✅ {rule.get('rule', rule)}")
            for rule in self.playbook.get("do_less", []):
                lines.append(f"- ❌ {rule.get('rule', rule)}")

        if self.science:
            where = f"ниша «{self.corpus_niche}»" if self.corpus_niche else "все ниши"
            lines += ["", f"### Что показал корпус залетевших роликов ({where}, сравнение с обычными роликами тех же авторов)"]
            for item in self.science:
                diff = f", разница {item['diff']:+}" if item.get("diff") is not None else ""
                q = f", q={item['q']}" if item.get("q") is not None else ""
                lines.append(f"- `{item['id']}` ({item['name']}): {item['status']} [литература {item['literature_level']}{diff}{q}]")
        if self.trends and (self.trends.get("rising") or self.trends.get("shifts")):
            lines += ["", f"### Что изменилось за последние {self.trends['window_days']:.0f} дней"]
            for item in self.trends.get("rising", [])[:4]:
                lines.append(f"- Чаще у залетевших: {item['label']} ({item['share_before']}% → {item['share_now']}%)")
            for item in self.trends.get("fading", [])[:3]:
                lines.append(f"- Реже у залетевших: {item['label']} ({item['share_before']}% → {item['share_now']}%)")
            for item in self.trends.get("shifts", [])[:3]:
                lines.append(f"- Сместился эффект: {item['label']} (δ {item['delta_before']:+} → {item['delta_now']:+})")

        if self.literature:
            lines += ["", self.literature]

        if self.n_results == 0 and not self.playbook:
            lines += ["", "Данных аккаунта пока нет — опирайся на базу знаний, рубрику и корпус."]
        return "\n".join(lines)


def build_context(store: MemoryStore, workspace: str) -> LearningContext:
    rows = store.training_rows(workspace)
    best, worst = select_examples(rows)
    insight = store.latest_insight(workspace)
    profile = store.get_profile(workspace)
    niche = corpus.match_niche(store, profile["niche"])
    report = corpus.latest_report(store, niche)
    science = [s for s in (report or {}).get("synthesis", []) if s["status"] not in ("нет данных", "гипотеза")][:8]
    return LearningContext(
        science=science,
        trends=(report or {}).get("trends"),
        corpus_niche=niche,
        literature=literature.prompt_block(store),
        workspace=workspace,
        profile=profile,
        calibration=calibrate(store, workspace),
        n_results=len(rows),
        lifts=trigger_lift(rows),
        best=best,
        worst=worst,
        playbook=insight["content"] if insight else None,
        pending=[
            {"id": h["id"], "text": h["text"], "created_at": h["created_at"]}
            for h in store.hooks_without_results(workspace, sources=SCORED_SOURCES, limit=5)
        ],
    )


def analyze(store: MemoryStore, workspace: str, criteria: dict[str, Any]) -> dict[str, Any]:
    """Score + predicted hold for one set of rubric scores under the current calibration."""
    cal = calibrate(store, workspace)
    return {
        "score": cal.score(criteria),
        "predicted_hold": cal.predict_hold(criteria),
        "calibration_scope": cal.scope,
        "calibration_n": cal.n,
        "calibration_mae": cal.mae,
    }
