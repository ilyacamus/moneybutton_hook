"""Knowledge base: psychological triggers, anti-patterns, formulas, actions, rubric.

The JSON files in ``data/`` are the single source of truth. Everything else
(the system prompt, the plugin's Markdown references) is rendered from them.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

GROUP_TITLES = {
    "stop": "Остановить палец (0–1 с)",
    "hold": "Удержать (1–3 с)",
    "trust": "Подкрепить доверие и ценность",
}

ACTION_CATEGORIES = {
    "sound": "Звук",
    "action": "Действие в кадре",
    "text": "Текст на экране",
    "edit": "Монтаж и камера",
}


@lru_cache(maxsize=None)
def _load(name: str) -> tuple[dict, ...]:
    with open(DATA_DIR / f"{name}.json", encoding="utf-8") as f:
        return tuple(json.load(f))


def triggers() -> tuple[dict, ...]:
    return _load("triggers")


def anti_patterns() -> tuple[dict, ...]:
    return _load("anti_patterns")


def formulas() -> tuple[dict, ...]:
    return _load("formulas")


def actions() -> tuple[dict, ...]:
    return _load("actions")


def rubric() -> tuple[dict, ...]:
    return _load("rubric")


@lru_cache(maxsize=None)
def principles() -> str:
    return (DATA_DIR / "principles.md").read_text(encoding="utf-8").strip()


def criterion_ids() -> list[str]:
    return [c["id"] for c in rubric()]


def prior_weights() -> dict[str, float]:
    return {c["id"]: float(c["weight"]) for c in rubric()}


def trigger_ids() -> list[str]:
    return [t["id"] for t in triggers()]


def anti_pattern_ids() -> list[str]:
    return [a["id"] for a in anti_patterns()]


def formula_ids() -> list[str]:
    return [f["id"] for f in formulas()]


def action_ids(category: str | None = None) -> list[str]:
    return [a["id"] for a in actions() if category is None or a["category"] == category]


def names(kind: str) -> dict[str, str]:
    """Map id -> human name for ``trigger``, ``anti_pattern``, ``action`` or ``criterion``."""
    source = {
        "trigger": triggers,
        "anti_pattern": anti_patterns,
        "action": actions,
        "criterion": rubric,
    }[kind]()
    return {item["id"]: item["name"] for item in source}


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #


def render_triggers() -> str:
    lines = ["# Психологические триггеры хука", ""]
    for group, title in GROUP_TITLES.items():
        lines += [f"## {title}", ""]
        for t in triggers():
            if t["group"] != group:
                continue
            lines += [
                f"### `{t['id']}` — {t['name']}",
                f"- **Механизм:** {t['mechanism']}",
                f"- **Как применить:** {t['how']}",
                "- **Примеры:** " + " / ".join(t["examples"]),
                f"- **Риск:** {t['risk']}",
                "",
            ]
    return "\n".join(lines).strip() + "\n"


def render_anti_patterns() -> str:
    lines = ["# Анти-паттерны (за что снижать оценку)", ""]
    for a in anti_patterns():
        lines += [f"- `{a['id']}` — **{a['name']}.** {a['description']} *Исправление:* {a['fix']}"]
    return "\n".join(lines).strip() + "\n"


def render_formulas() -> str:
    lines = ["# Формулы хуков", ""]
    for f in formulas():
        trig = ", ".join(f"`{t}`" for t in f["trigger_ids"])
        lines += [
            f"- `{f['id']}` — **{f['template']}**. Пример: «{f['example']}». "
            f"Триггеры: {trig}. Подходит: {f['best_for']}."
        ]
    return "\n".join(lines).strip() + "\n"


def render_actions() -> str:
    lines = ["# Библиотека действий для первых 3 секунд", ""]
    for category, title in ACTION_CATEGORIES.items():
        lines += [f"## {title} (`{category}`)", ""]
        for a in actions():
            if a["category"] != category:
                continue
            lines += [
                f"- `{a['id']}` — **{a['name']}** ({a['timing']}). {a['how']} Когда: {a['when']}."
            ]
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_rubric() -> str:
    lines = [
        "# Рубрика оценки хука",
        "",
        "Каждый критерий оценивается целым числом от 0 до 10. Итоговый балл 0–100 = 10 × взвешенная сумма. "
        "Веса ниже — стартовые (априорные); самообучение пересчитывает их по фактическому удержанию.",
        "",
        "Калибровка шкалы: средний хук из ленты — 4–5; 7 — сильный; 9–10 — только если хук почти наверняка "
        "остановит целевого зрителя.",
        "",
        "| id | Критерий | Вес | Вопрос | 9–10 | 0–3 |",
        "|---|---|---|---|---|---|",
    ]
    for c in rubric():
        lines.append(
            f"| `{c['id']}` | {c['name']} | {c['weight']:.2f} | {c['question']} | {c['high']} | {c['low']} |"
        )
    return "\n".join(lines).strip() + "\n"


def render_principles() -> str:
    return principles() + "\n"


RENDERERS = {
    "principles.md": render_principles,
    "rubric.md": render_rubric,
    "triggers.md": render_triggers,
    "anti-patterns.md": render_anti_patterns,
    "formulas.md": render_formulas,
    "actions.md": render_actions,
}


def render_all() -> dict[str, str]:
    """Render every knowledge file as Markdown, keyed by file name."""
    return {name: render() for name, render in RENDERERS.items()}


def render_knowledge_base() -> str:
    """The whole knowledge base as one Markdown document (used in the system prompt)."""
    return "\n\n".join(render_all().values())
