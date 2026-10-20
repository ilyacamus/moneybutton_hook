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
