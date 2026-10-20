"""Structured-output schemas for every engine task.

Ids are constrained to the knowledge base with ``Literal`` enums, so the model
can only reference triggers, anti-patterns, formulas and actions that exist.
Numeric ranges live in descriptions (the API strips min/max) and are clamped
in code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, create_model

from .core import knowledge

TriggerId = Literal[tuple(knowledge.trigger_ids())]  # type: ignore[valid-type]
AntiPatternId = Literal[tuple(knowledge.anti_pattern_ids())]  # type: ignore[valid-type]
FormulaId = Literal[tuple([*knowledge.formula_ids(), "custom"])]  # type: ignore[valid-type]
SoundId = Literal[tuple(knowledge.action_ids("sound"))]  # type: ignore[valid-type]
FrameActionId = Literal[tuple(knowledge.action_ids("action"))]  # type: ignore[valid-type]
TextActionId = Literal[tuple(knowledge.action_ids("text"))]  # type: ignore[valid-type]
EditActionId = Literal[tuple(knowledge.action_ids("edit"))]  # type: ignore[valid-type]


class CriterionScore(BaseModel):
    score: int = Field(description="Целое число от 0 до 10 по калибровке шкалы из рубрики")
    reason: str = Field(description="Одна короткая фраза: почему такая оценка")


CriteriaScores = create_model(
    "CriteriaScores",
    **{
        c["id"]: (CriterionScore, Field(description=f"{c['name']}. {c['question']}"))
        for c in knowledge.rubric()
    },
)


class HookVariant(BaseModel):
    spoken: str = Field(description="Что произносится в первые 3 секунды, дословно")
    on_screen_text: str = Field(description="Текст на экране, 3–7 слов")
    visual: str = Field(description="Что происходит в кадре с 0.0 до 3.0 секунды")
    formula_id: FormulaId = Field(description="id формулы из базы знаний или custom")
    trigger_ids: list[TriggerId] = Field(description="1–3 главных триггера этого варианта")
    why: str = Field(description="Почему этот вариант удержит целевого зрителя, одно предложение")


class Evaluation(BaseModel):
    verdict: str = Field(description="Вердикт одной фразой")
    scores: CriteriaScores  # type: ignore[valid-type]
    trigger_ids: list[TriggerId] = Field(description="Триггеры, которые реально работают в хуке")
    anti_pattern_ids: list[AntiPatternId] = Field(description="Найденные анти-паттерны")
    first_second: str = Field(description="Что зритель увидит и услышит в первую секунду")
    swipe_risk: str = Field(description="В какой момент и почему зритель вероятнее всего свайпнет")
    strengths: list[str] = Field(description="1–3 сильные стороны")
    weaknesses: list[str] = Field(description="1–3 главные слабости")
    improved: list[HookVariant] = Field(description="Ровно 3 улучшенные версии хука")


class Generation(BaseModel):
    core_promise: str = Field(description="Главное обещание ролика, которое должен продать хук")
    audience: str = Field(description="Кто целевой зритель")
    hooks: list[HookVariant] = Field(description="Варианты хуков на разных формулах и триггерах")
    top_pick: int = Field(description="Индекс лучшего варианта в списке hooks, с 0")
    top_pick_reason: str


class Rewrite(BaseModel):
    diagnosis: str = Field(description="Почему исходный хук теряет зрителя в первые 3 секунды")
    keep: str = Field(description="Что в исходном хуке стоит сохранить")
    rewrites: list[HookVariant] = Field(description="Ровно 5 переписанных версий в разных направлениях")
    top_pick: int = Field(description="Индекс лучшей версии в списке rewrites, с 0")
    top_pick_reason: str


class SoundStep(BaseModel):
    action_id: SoundId
    instruction: str = Field(description="Конкретно для этого ролика: что именно сделать")
    timing: str = Field(description="Окно времени, например 0.0–0.3 с")
    why: str


class FrameStep(BaseModel):
    action_id: FrameActionId
    instruction: str = Field(description="Конкретно для этого ролика: что именно сделать")
    timing: str = Field(description="Окно времени, например 0.0–0.3 с")
    why: str


class TextStep(BaseModel):
    action_id: TextActionId
    text: str = Field(description="Готовый текст для экрана")
    placement: str = Field(description="Где и как разместить: зона экрана, размер, анимация")
    timing: str = Field(description="Окно времени, например 0.0–1.5 с")
    why: str


class EditStep(BaseModel):
    action_id: EditActionId
    instruction: str = Field(description="Конкретно для этого ролика: что именно сделать")
    timing: str = Field(description="Окно времени, например 1.0–1.5 с")
    why: str


class TimelineBeat(BaseModel):
    window: str = Field(description="0.0–0.5 с, 0.5–1.5 с или 1.5–3.0 с")
    visual: str
    audio: str
    on_screen_text: str


class ActionPlan(BaseModel):
    summary: str = Field(description="Главная идея усиления первых 3 секунд, одно предложение")
    sound: list[SoundStep] = Field(description="2–3 варианта звука")
    action: list[FrameStep] = Field(description="2–3 варианта действия в кадре")
    text: list[TextStep] = Field(description="2–3 варианта текста на экране")
    edit: list[EditStep] = Field(description="1–2 монтажных приёма")
    timeline: list[TimelineBeat] = Field(description="Рекомендуемая сборка: ровно 3 окна")


class PlaybookRule(BaseModel):
    rule: str = Field(description="Правило в повелительном наклонении")
    evidence: str = Field(description="Цифры из данных, на которых основано правило")
    confidence: Literal["low", "medium", "high"]


class Playbook(BaseModel):
    summary: str = Field(description="Главный вывод о том, что удерживает аудиторию этого аккаунта")
    do_more: list[PlaybookRule]
    do_less: list[PlaybookRule]
    experiments: list[str] = Field(description="2–4 A/B-теста хуков для следующих роликов")
