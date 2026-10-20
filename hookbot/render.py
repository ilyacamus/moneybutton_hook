"""Human-readable reports, shared by the Telegram bot (HTML) and the CLI (plain text)."""

from __future__ import annotations

import html
from typing import Any

from .core import knowledge
from .core.learning import MIN_RESULTS, Calibration, LearningContext
from .engine import EvaluationReport, ResultOutcome, VariantsReport
from .schemas import ActionPlan, HookVariant


class Plain:
    @staticmethod
    def esc(text: Any) -> str:
        return str(text)

    @classmethod
    def b(cls, text: Any) -> str:
        return cls.esc(text)

    @classmethod
    def i(cls, text: Any) -> str:
        return cls.esc(text)

    @classmethod
    def code(cls, text: Any) -> str:
        return cls.esc(text)


class HTML(Plain):
    @staticmethod
    def esc(text: Any) -> str:
        return html.escape(str(text), quote=False)

    @classmethod
    def b(cls, text: Any) -> str:
        return f"<b>{cls.esc(text)}</b>"

    @classmethod
    def i(cls, text: Any) -> str:
        return f"<i>{cls.esc(text)}</i>"

    @classmethod
    def code(cls, text: Any) -> str:
        return f"<code>{cls.esc(text)}</code>"


def bar(score: float, width: int = 10) -> str:
    filled = max(0, min(width, round(score / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)


def verdict_label(score: float | None) -> str:
    if score is None:
        return "—"
    if score >= 75:
        return "🔥 Сильный хук"
    if score >= 60:
        return "👍 Рабочий хук"
    if score >= 45:
        return "⚠️ Слабый хук"
    return "🛑 Пролистают"


def hold_line(predicted: float | None, cal: Calibration, f: type[Plain] = HTML) -> str:
    if predicted is None:
        left = max(0, MIN_RESULTS - cal.n)
        return f.i(
            f"Прогноз удержания 3 с появится после {MIN_RESULTS} роликов с результатами "
            f"(осталось {left}). Вносите метрики — бот обучается на вашей аудитории."
        )
    source = "ваших роликах" if cal.scope == "workspace" else "общей базе"
    mae = f", ±{cal.mae:.0f} п.п." if cal.mae is not None else ""
    return (
        f"📈 Прогноз удержания 3 с: {f.b(f'{predicted:.0f}%')} (skip rate ≈ {100 - predicted:.0f}%)\n"
        + f.i(f"Калибровка на {cal.n} {source}{mae}")
    )


def _ids(ids: list[str], kind: str) -> str:
    names = knowledge.names(kind)
    return ", ".join(names.get(i, i) for i in ids)


def render_variant(index: int, v: HookVariant, hook_id: int | None, f: type[Plain] = HTML, star: bool = False) -> str:
    head = f"{'⭐ ' if star else ''}{index}. " + (f.code(f"#{hook_id}") + " " if hook_id else "")
    lines = [head + f.b(v.spoken)]
    if v.on_screen_text:
        lines.append(f"   📝 {f.esc(v.on_screen_text)}")
    if v.visual:
        lines.append(f"   🎬 {f.esc(v.visual)}")
    lines.append(f"   🧠 {f.i(_ids(list(v.trigger_ids), 'trigger'))} — {f.esc(v.why)}")
    return "\n".join(lines)


def render_evaluation(report: EvaluationReport, f: type[Plain] = HTML) -> str:
    ev = report.evaluation
    score = report.score or 0
    lines = []
    if report.hook_id:
        lines.append(f"Хук {f.code(f'#{report.hook_id}')}")
    lines += [
        f"{f.b(verdict_label(report.score))}  {bar(score)} {f.b(f'{score:.0f}/100')}",
        f.i(ev.verdict),
        "",
        hold_line(report.predicted_hold, report.calibration, f),
        "",
        f"👁 {f.b('Первая секунда:')} {f.esc(ev.first_second)}",
        f"👋 {f.b('Где свайпнут:')} {f.esc(ev.swipe_risk)}",
        "",
        f.b("Оценка по критериям"),
    ]
    for c in knowledge.rubric():
        item = getattr(ev.scores, c["id"])
        value = report.criteria[c["id"]]
        lines.append(f"{'🟢' if value >= 7 else '🟡' if value >= 5 else '🔴'} {f.esc(c['name'])}: {f.b(value)} — {f.esc(item.reason)}")
    if ev.trigger_ids:
        lines += ["", f"🧠 {f.b('Триггеры:')} {f.esc(_ids(list(ev.trigger_ids), 'trigger'))}"]
    if ev.anti_pattern_ids:
        lines.append(f"🚫 {f.b('Анти-паттерны:')} {f.esc(_ids(list(ev.anti_pattern_ids), 'anti_pattern'))}")
    if ev.strengths:
        lines += ["", f.b("Сильные стороны")] + [f"✅ {f.esc(s)}" for s in ev.strengths]
    if ev.weaknesses:
        lines += ["", f.b("Что мешает")] + [f"❌ {f.esc(s)}" for s in ev.weaknesses]
    if report.video_facts:
        lines += ["", f.b("Факты о видео"), f.esc(report.video_facts["text"])]
    if report.benchmark:
        lines += ["", *render_benchmark(report.benchmark, f)]
    if ev.improved:
        lines += ["", f.b("Улучшенные версии")]
        ids = report.improved_ids or [None] * len(ev.improved)
        for n, (v, hid) in enumerate(zip(ev.improved, ids), 1):
            lines += [render_variant(n, v, hid, f), ""]
    return "\n".join(lines).strip()


def render_variants(report: VariantsReport, f: type[Plain] = HTML) -> str:
    res = report.result
    lines = []
    if hasattr(res, "core_promise"):
        lines += [
            f"🎯 {f.b('Обещание ролика:')} {f.esc(res.core_promise)}",
            f"👥 {f.b('Зритель:')} {f.esc(res.audience)}",
        ]
    else:
        lines += [
            f"🩺 {f.b('Диагноз:')} {f.esc(res.diagnosis)}",
            f"💎 {f.b('Сохранить:')} {f.esc(res.keep)}",
        ]
    lines.append("")
    for n, (v, hid) in enumerate(zip(report.variants, report.hook_ids), 1):
        lines += [render_variant(n, v, hid, f, star=(n - 1 == res.top_pick)), ""]
    lines.append(f"⭐ {f.b('Лучший вариант:')} {f.esc(res.top_pick_reason)}")
    return "\n".join(lines).strip()


def render_actions(plan: ActionPlan, f: type[Plain] = HTML) -> str:
    names = knowledge.names("action")
    lines = [f"💡 {f.esc(plan.summary)}", ""]
    sections = [
        ("🔊 Звук", plan.sound),
        ("🎬 Действие в кадре", plan.action),
        ("📝 Текст на экране", plan.text),
        ("✂️ Монтаж", plan.edit),
    ]
    for title, steps in sections:
        if not steps:
            continue
        lines.append(f.b(title))
        for n, step in enumerate(steps, 1):
            what = f"«{step.text}» — {step.placement}" if hasattr(step, "text") else step.instruction
            lines.append(
                f"{n}) {f.b(names.get(step.action_id, step.action_id))} [{f.esc(step.timing)}]: "
                f"{f.esc(what)} — {f.i(step.why)}"
            )
        lines.append("")
    if plan.timeline:
        lines.append(f.b("⏱ Сборка первых 3 секунд"))
        for beat in plan.timeline:
            lines.append(
                f"{f.b(beat.window)}\n   🎬 {f.esc(beat.visual)}\n   🔊 {f.esc(beat.audio)}\n   📝 {f.esc(beat.on_screen_text)}"
            )
    return "\n".join(lines).strip()


def render_result(outcome: ResultOutcome, f: type[Plain] = HTML) -> str:
    m = outcome.metrics
    lines = [f"✅ Результат для хука {f.code('#' + str(outcome.hook['id']))} сохранён."]
    if m.get("hold_3s") is not None:
        hold, skip = m["hold_3s"], m["skip_rate"]
        lines.append(f"Удержание 3 с: {f.b(f'{hold:.0f}%')} (skip rate {skip:.0f}%)")
    if outcome.prediction_error is not None:
        sign = "+" if outcome.prediction_error >= 0 else ""
        lines.append(
            f"Прогноз был {outcome.predicted_hold:.0f}% → ошибка {sign}{outcome.prediction_error} п.п."
        )
    extra = [f"{label}: {m[key]}" for key, label in
             (("views", "просмотры"), ("avg_watch_sec", "ср. просмотр, с"), ("shares", "отправки"), ("saves", "сохранения"))
             if m.get(key) is not None]
    if extra:
        lines.append(" · ".join(extra))
    cal = outcome.calibration
    lines.append("")
    if cal.scope == "prior":
        lines.append(f.i(f"Роликов с результатами: {outcome.n_results}. Калибровка включится с {MIN_RESULTS}."))
    else:
        lines.append(f.i(f"Модель переобучена на {cal.n} роликах" + (f", точность ±{cal.mae:.0f} п.п." if cal.mae else ".")))
    return "\n".join(lines)


def render_stats(ctx: LearningContext, f: type[Plain] = HTML) -> str:
    cal = ctx.calibration
    names = knowledge.names("criterion")
    lines = [f.b("🧠 Чему бот научился на ваших роликах"), ""]
    lines.append(f"Роликов с результатами: {f.b(ctx.n_results)}")
    if cal.scope == "prior":
        lines.append(f"Калибровка: экспертная рубрика (личная включится с {MIN_RESULTS} результатов)")
    else:
        lines.append(
            f"Калибровка: {'ваши ролики' if cal.scope == 'workspace' else 'общая база'}, n={cal.n}, "
            f"среднее удержание {cal.y_mean:.0f}%" + (f", точность ±{cal.mae:.0f} п.п." if cal.mae else "")
        )
        lines += ["", f.b("Что важнее всего для вашей аудитории")]
        for cid, weight in sorted(cal.weights.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"{bar(weight * 100 * 3, 8)} {f.esc(names[cid])} — {weight * 100:.0f}%")
    if ctx.lifts:
        lines += ["", f.b("Триггеры и удержание")]
        for item in ctx.lifts[:6]:
            sign = "+" if item["lift"] >= 0 else ""
            lines.append(f"{'📈' if item['lift'] >= 0 else '📉'} {f.esc(item['name'])}: {sign}{item['lift']} п.п. (n={item['n']})")
    if ctx.best:
        lines += ["", f.b("Лучшие хуки")]
        lines += [f"{ex['hold_3s']:.0f}% — «{f.esc(ex['text'])}»" for ex in ctx.best[:3]]
    if ctx.pending:
        lines += ["", f.b("Ждут результатов")]
        lines += [f"{f.code('#' + str(p['id']))} {f.esc(p['text'][:70])}" for p in ctx.pending]
    return "\n".join(lines)


def render_playbook(playbook: dict[str, Any] | None, f: type[Plain] = HTML) -> str:
    if not playbook:
        return "Плейбука пока нет: нужно хотя бы 3 ролика с результатами."
    lines = [f.b("📘 Ваш плейбук хуков"), f.esc(playbook.get("summary", "")), ""]
    marks = {"high": "●●●", "medium": "●●○", "low": "●○○"}
    for title, key, icon in (("Делать больше", "do_more", "✅"), ("Делать меньше", "do_less", "❌")):
        rules = playbook.get(key) or []
        if rules:
            lines.append(f.b(title))
            for r in rules:
                lines.append(f"{icon} {f.esc(r['rule'])} {marks.get(r.get('confidence'), '')}\n   {f.i(r.get('evidence', ''))}")
            lines.append("")
    if playbook.get("experiments"):
        lines.append(f.b("🧪 Что протестировать"))
        lines += [f"• {f.esc(e)}" for e in playbook["experiments"]]
    return "\n".join(lines).strip()


def render_benchmark(bench: dict[str, Any], f: type[Plain] = HTML) -> list[str]:
    where = "все ниши" if bench["niche"] == "*" else f"ниша «{bench['niche']}»"
    lines = [f.b(f"📊 Сравнение с залетевшими ({where}: {bench['n_viral']} залетевших, {bench['n_control']} обычных)")]
    score = bench.get("score")
    if score:
        control = f", обычные — {score['control_median']:.0f}" if score.get("control_median") is not None else ""
        lines.append(f"Балл {score['value']:.0f}: выше, чем у {score['pct_of_viral']}% залетевших "
                     f"(их медиана {score['viral_median']:.0f}{control})")
    for item in bench["items"]:
        if item["kind"] == "binary":
            mark = "✅" if item["value"] else "▫️"
            control = f", у обычных {item['control_share']}%" if item.get("control_share") is not None else ""
            lines.append(f"{mark} {f.esc(item['label'])}: у залетевших {item['viral_share']}%{control}")
        else:
            lines.append(f"• {f.esc(item['label'])}: у вас {item['value']:g}, медиана залетевших "
                         f"{item['viral_median']:g}" + (f", обычных {item['control_median']:g}" if item.get("control_median") is not None else ""))
    return lines


STATUS_ICONS = {
    "доказано: наука + данные": "🟢",
    "подтверждено данными (новое знание)": "🔵",
    "наука за, на данных пока не видно": "🟡",
    "данные против науки — проверить, возможен сдвиг тренда": "🟠",
    "данные против практики": "🟠",
    "гипотеза": "⚪",
    "нет данных": "▫️",
}


def render_science(report: dict[str, Any] | None, f: type[Plain] = HTML, *, limit: int = 12) -> str:
    """The corpus report: what actually separates viral reels from the same creators' typical ones."""
    if not report:
        return ("Корпус залетевших роликов пока пуст. Робот начнёт учиться, когда появятся отслеживаемые аккаунты "
                "(/watch) или присланные эталоны (/ref).")
    where = "все ниши" if report["niche"] == "*" else f"ниша «{report['niche']}»"
    v = report["validity"]
    lines = [f.b(f"🔬 Что реально работает — {where}"),
             f.i("Сравниваем залетевшие ролики (≥3× медианы автора) с обычными роликами тех же авторов; "
                 "оценка вслепую; поправка на множественные проверки (q-value).")]
    lines.append(f"Изучено: {v['n_viral']} залетевших, {v['n_control']} обычных.")
    if v.get("auc") is not None:
        lo, hi = v["auc_ci"]
        lines.append(f"Рубрика отличает залетевшие от обычных: AUC {v['auc']:.2f} (95% ДИ {lo:.2f}–{hi:.2f}; 0.5 = угадывание)")
    lines += ["", f.b("Наука + данные")]
    shown = 0
    for item in report.get("synthesis", []):
        if item["status"] in ("нет данных",) or shown >= limit:
            continue
        stat = f" — разница {item['diff']:+g}, q={item['q']}" if item.get("q") is not None else ""
        lines.append(f"{STATUS_ICONS.get(item['status'], '•')} {f.esc(item['name'])} "
                     f"[литература {item['literature_level']}]: {f.esc(item['status'])}{stat}")
        shown += 1
    trends = report.get("trends") or {}
    if trends.get("rising") or trends.get("fading") or trends.get("shifts"):
        lines += ["", f.b(f"📈 Тренды: последние {trends['window_days']:.0f} дней против раньше")]
        for item in trends.get("rising", [])[:5]:
            lines.append(f"⬆️ {f.esc(item['label'])}: {item['share_before']}% → {item['share_now']}% залетевших")
        for item in trends.get("fading", [])[:5]:
            lines.append(f"⬇️ {f.esc(item['label'])}: {item['share_before']}% → {item['share_now']}% залетевших")
        for item in trends.get("shifts", [])[:5]:
            lines.append(f"🔄 {f.esc(item['label'])}: эффект {item['delta_before']:+} → {item['delta_now']:+}")
    w = report.get("weights")
    if w:
        names = knowledge.names("criterion")
        top = sorted(w["weights"].items(), key=lambda kv: -kv[1])[:3]
        lines += ["", f.i(f"Веса рубрики по корпусу (n={w['n']}, свежие ролики весят больше): "
                          + ", ".join(f"{names[c]} {v * 100:.0f}%" for c, v in top))]
    return "\n".join(lines)


def render_validation(report: dict[str, Any], f: type[Plain] = HTML) -> str:
    lines = [f.b("🧪 Насколько можно доверять оценщику")]
    fa = report.get("forecast_account") or report.get("forecast_all") or {}
    if fa.get("mae") is not None:
        skill = fa["skill"]
        verdict = "лучше наивного прогноза" if skill and skill > 0 else "пока не лучше наивного прогноза"
        rho = f", ранговая корреляция {fa['spearman']:+.2f}" if fa.get("spearman") is not None else ""
        lines.append(f"Прогноз удержания 3 с: ошибка ±{fa['mae']} п.п. против ±{fa['baseline_mae']} у среднего "
                     f"по аккаунту (n={fa['n']}) — {verdict}{rho}")
    else:
        lines.append("Прогноз удержания: мало проверенных прогнозов (нужно ≥3 ролика, оценённых до публикации).")
    c = report.get("corpus")
    if c and c.get("auc") is not None:
        lines.append(f"Различение залетевших и обычных роликов: AUC {c['auc']:.2f} (95% ДИ {c['auc_ci'][0]:.2f}–{c['auc_ci'][1]:.2f})")
    else:
        lines.append("Различение залетевших: корпус ещё мал.")
    r = report.get("reliability")
    if r and r.get("score_icc") is not None:
        lines.append(f"Надёжность оценок (повторная оценка {r['n_hooks']} хуков × {r['raters']}): "
                     f"ICC {r['score_icc']:.2f} — {r['score_band']}")
    else:
        lines.append("Надёжность оценок: проверка не запускалась.")
    return "\n".join(lines)


def render_papers(papers: list[dict[str, Any]], f: type[Plain] = HTML, *, numbered: bool = True) -> str:
    if not papers:
        return "Новых исследований, ждущих проверки, нет."
    lines = []
    for n, p in enumerate(papers, 1):
        head = f"{n}. " if numbered else "• "
        review = "" if p.get("peer_reviewed") else " (препринт)"
        lines.append(f"{head}{f.b(p['title'])} — {f.esc(p['authors'])}, {p['year']}, {f.esc(p['venue'])}{review} "
                     f"[{p['evidence_level']}]\n   {f.esc(p['finding'])}\n   {f.esc(p['link'])}")
    return "\n".join(lines)


def render_reel_analysis(data: dict[str, Any], reel: dict[str, Any] | None = None, f: type[Plain] = HTML) -> str:
    names = knowledge.names("criterion")
    lines = [f.b("🎬 Разбор ролика (вслепую, без учёта просмотров)")]
    if reel and reel.get("outlier_score"):
        lines.append(f"Просмотры: в {reel['outlier_score']:.1f}× больше обычного у автора — {reel['label']}")
    if data.get("spoken_hook"):
        lines.append(f"🗣 {f.esc(data['spoken_hook'])}")
    if data.get("on_screen_text"):
        lines.append(f"📝 {f.esc(data['on_screen_text'])}")
    lines.append(f"🎞 {f.esc(data.get('visual', ''))}")
    flags = [label for key, label in (("face_first_second", "лицо"), ("text_first_second", "текст"),
                                      ("motion_first_second", "движение")) if data.get(key)]
    lines.append("В первую секунду: " + (", ".join(flags) if flags else "ни лица, ни текста, ни движения"))
    crit = data.get("criteria") or {}
    if crit:
        top = sorted(crit.items(), key=lambda kv: -kv[1])[:3]
        lines.append("Сильнее всего: " + ", ".join(f"{names[c]} {v}" for c, v in top))
    if data.get("trigger_ids"):
        lines.append("🧠 " + f.esc(_ids(list(data["trigger_ids"]), "trigger")))
    if data.get("hypothesis"):
        lines.append(f"💡 {f.esc(data['hypothesis'])}")
    return "\n".join(lines)


def render_audience(report: dict[str, Any] | None, f: type[Plain] = HTML) -> str:
    """The channel audit: what holds this creator's own audience."""
    if not report:
        return "Аудита канала ещё не было. Запустите /audit или пришлите свои ролики через /myreel."
    lines = [f.b("👥 Аудит вашего канала: что удерживает вашу аудиторию")]
    lines.append(f"Роликов: {report['n_reels']}, разобрано: {report['n_studied']}, со skip rate: {report['n_with_skip_rate']}")
    if report.get("median_skip_rate") is not None:
        lines.append(f"Медианный skip rate: {report['median_skip_rate']}% (удержание 3 с ≈ {100 - report['median_skip_rate']:.0f}%)")
    if report.get("median_views"):
        lines.append(f"Медиана просмотров: {report['median_views']:,.0f}".replace(",", " ")
                     + f"; прорывных роликов (≥3× медианы): {report['breakouts']}")
    if report.get("median_engagement") is not None:
        lines.append(f"Медиана лайков и комментариев: {report['median_engagement']} на 100 просмотров")
    if report["n_studied"] < report["min_reels"]:
        lines.append(f.i(f"Для статистики нужно хотя бы {report['min_reels']} разобранных роликов. "
                         "Выводы ниже — предварительные."))
    if not report["n_with_skip_rate"] and report["n_studied"]:
        lines.append(f.i("Skip rate нет (он виден только вам в статистике): анализ по просмотрам и вовлечённости. "
                         "Добавьте таблицу со skip rate — появится анализ удержания 3 с."))

    def effects(title: str, items: list[dict[str, Any]], kind: str) -> list[str]:
        out = []
        for e in items:
            if e["verdict"] == "не подтверждено":
                continue
            if kind == "hold":
                extra = (f" ({e['hold_with']}% vs {e['hold_without']}%)" if "hold_with" in e else "")
                out.append(f"{'⬆️' if e['rho'] > 0 else '⬇️'} {f.esc(e['label'])}: ρ={e['rho']:+.2f}{extra}, q={e['q']} — {e['verdict']}")
            else:
                out.append(f"{'⬆️' if e['diff'] > 0 else '⬇️'} {f.esc(e['label'])}: прорывные {e['viral']} vs обычные "
                           f"{e['control']}, q={e['q']} — {e['verdict']}")
        return ([f.b(title)] + out[:8]) if out else []

    def public(title: str, items: list[dict[str, Any]], outcome: str, unit: str) -> list[str]:
        out = []
        for e in items:
            if e["verdict"] == "не подтверждено":
                continue
            extra = (f" ({e[outcome + '_with']}{unit} vs {e[outcome + '_without']}{unit})" if f"{outcome}_with" in e else "")
            out.append(f"{'⬆️' if e['rho'] > 0 else '⬇️'} {f.esc(e['label'])}: ρ={e['rho']:+.2f}{extra}, q={e['q']} — {e['verdict']}")
        return ([f.b(title)] + out[:8]) if out else []

    blocks = [b for b in (
        effects("Что связано с удержанием первых 3 секунд", report["hold_effects"], "hold"),
        public("Что связано с просмотрами (× медианы канала)", report.get("views_effects", []), "views", "×"),
        public("Что связано с лайками и комментариями на просмотр", report.get("engagement_effects", []), "engagement", "%"),
        public("Что связано с сохранениями на просмотр", report.get("saves_effects", []), "saves", "%"),
        effects("Что отличает ваши прорывные ролики", report["reach_effects"], "reach"),
    ) if b]
    if blocks:
        for block in blocks:
            lines += ["", *block]
    else:
        lines += ["", f.i("Статистически значимых закономерностей пока нет — нужно больше роликов.")]
    agreed = [s for s in report.get("hold_synthesis", []) + report.get("views_synthesis", []) + report.get("reach_synthesis", [])
              if s["status"] not in ("нет данных", "гипотеза")]
    if agreed:
        lines += ["", f.b("Ваша аудитория и наука")]
        seen = set()
        for s in agreed:
            if s["id"] in seen:
                continue
            seen.add(s["id"])
            lines.append(f"{STATUS_ICONS.get(s['status'], '•')} {f.esc(s['name'])} [литература {s['literature_level']}]: {f.esc(s['status'])}")
    if report.get("best_hold"):
        lines += ["", f.b("Лучшие хуки по удержанию")]
        lines += [f"{r['skip_rate']:.0f}% skip — «{f.esc((r['hook'] or '')[:90])}»" for r in report["best_hold"]]
    if report.get("worst_hold"):
        lines += ["", f.b("Хуки, которые пролистывают")]
        lines += [f"{r['skip_rate']:.0f}% skip — «{f.esc((r['hook'] or '')[:90])}»" for r in report["worst_hold"]]
    return "\n".join(lines)


def render_audience_profile(saved: dict[str, Any] | None, f: type[Plain] = HTML) -> str:
    if not saved:
        return "Профиля аудитории ещё нет. Сначала аудит (/audit или /myreel), затем /target."
    p, creators = saved["profile"], saved.get("creators", [])
    conf = {"low": "низкая", "medium": "средняя", "high": "высокая"}[p["confidence"]]
    lines = [f.b("🎯 Целевая аудитория канала"), f.esc(p["summary"]), f.i(f"Уверенность: {conf}"), ""]
    if p["demographics"]:
        lines.append(f"👥 {f.esc(p['demographics'])}")
    for seg in p["segments"]:
        lines.append(f"• {f.b(seg['name'])} ({seg['weight']}): {f.esc(seg['description'])}")
    for title, key in (("Интересы", "interests"), ("Боли", "pains"), ("Желания", "desires"),
                       ("Что у них работает", "resonating_content")):
        if p.get(key):
            lines.append(f"{f.b(title + ':')} {f.esc('; '.join(p[key]))}")
    lines.append(f"{f.b('Как говорят:')} {f.esc(p['language_style'])}")
    if p.get("data_gaps"):
        lines.append(f.i("Не хватает данных: " + "; ".join(p["data_gaps"])))
    if creators:
        ok = [c for c in creators if c.get("verified")]
        bad = [c for c in creators if c.get("verified") is False]
        unchecked = [c for c in creators if c.get("verified") is None]
        lines += ["", f.b(f"🔭 Авторы для этой аудитории: проверено {len(ok)}, отклонено {len(bad)}, не проверено {len(unchecked)}")]
        for c in ok[:12]:
            followers = f", {c['followers']:,} подписчиков".replace(",", " ") if c.get("followers") else ""
            lines.append(f"✅ @{f.esc(c['username'])}{followers} — {f.esc(c['segment'])}")
        for c in unchecked[:8]:
            lines.append(f"❔ @{f.esc(c['username'])} — {f.esc(c['segment'])} (нужна проверка через Instagram API)")
    return "\n".join(lines)


def render_gap(gap: dict[str, Any] | None, f: type[Plain] = HTML) -> str:
    if not gap:
        return "Сравнения с контентом для вашей аудитории ещё нет: запустите /target."
    lines = [f.b("🧭 Ваш канал против залетевшего контента для вашей аудитории"),
             f.i(f"{gap['n_creators']} авторов: {gap['n_viral']} залетевших и {gap['n_typical']} обычных роликов; "
                 f"ваших роликов: {gap['n_own']}. Разбор вслепую, те же критерии хуков.")]
    recs = gap.get("recommendations") or []
    if recs:
        lines += ["", f.b("Что делать (данные аудитории + ваш аудит + наука)")]
        for r in recs[:8]:
            unit = "%" if r["kind"] == "binary" else ""
            lines.append(f"{'⬆️' if r['do'] == 'больше' else '⬇️'} {f.b(r['do'].capitalize() + ': ' + r['label'])}\n"
                         f"   залетевшие {r['audience_viral']}{unit} · обычные {r['audience_typical']}{unit} · у вас {r['own']}{unit} "
                         f"(q={r['audience_q']})\n   {f.esc(r['own_audit'])}"
                         + (f"; {f.esc(r['literature'])}" if r.get("literature") else ""))
    else:
        lines += ["", f.i("Подтверждённых разрывов пока нет: нужно больше изученных роликов этой аудитории.")]
    big = [g for g in gap.get("gaps", []) if g["verdict"] != "нет разрыва"][:6]
    if big:
        lines += ["", f.b("Чем ваши ролики отличаются от залетевших у вашей аудитории")]
        for g in big:
            unit = "%" if g["kind"] == "binary" else ""
            lines.append(f"• {f.esc(g['label'])}: залетевшие {g['audience_viral']}{unit}, у вас {g['own']}{unit} — {g['verdict']}")
    if gap.get("exemplars"):
        lines += ["", f.b("Залетевшие хуки для вашей аудитории")]
        for ex in gap["exemplars"][:5]:
            lines.append(f"🔥 {ex['outlier_score']:.1f}× — «{f.esc(ex['hook'][:100])}» {f.esc(ex['url'])}")
    return "\n".join(lines)


def render_connection(steps: list[dict[str, Any]], f: type[Plain] = HTML) -> str:
    icons = {True: "✅", None: "⚠️", False: "❌"}
    lines = [f.b("🔌 Подключение Instagram")]
    for s in steps:
        lines.append(f"{icons[s['ok']]} {f.b(s['step'])}: {f.esc(s['detail'])}")
        if s["fix"]:
            lines.append(f"   → {f.esc(s['fix'])}")
    blocking = [s for s in steps if s["ok"] is False]
    lines += ["", "Можно запускать аудит и поиск аудитории." if not blocking
              else f"Исправьте {len(blocking)} ❌ — без этого аудит или поиск авторов не заработает."]
    return "\n".join(lines)
