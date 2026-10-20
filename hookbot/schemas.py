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


ReelFormat = Literal["talking_head", "voiceover_broll", "tutorial", "skit", "pov", "before_after", "product",
                     "vlog", "text_on_screen", "other"]


class ReelAnalysis(BaseModel):
    """Blind analysis of a published reel's first 3 seconds (the model never sees its views)."""

    spoken_hook: str = Field(description="Что говорится в первые 3 секунды: из расшифровки, подписи или текста на экране; пусто, если неизвестно")
    spoken_source: Literal["transcript", "caption", "on_screen", "unknown"]
    on_screen_text: str = Field(description="Текст на экране в первых кадрах дословно, пусто если нет")
    visual: str = Field(description="Что происходит в кадре с 0.0 до 3.0 секунды")
    format: ReelFormat
    face_first_second: bool = Field(description="Есть ли лицо крупно в первую секунду")
    text_first_second: bool = Field(description="Есть ли текст на экране в первую секунду")
    motion_first_second: bool = Field(description="Есть ли заметное движение в первую секунду")
    scores: CriteriaScores  # type: ignore[valid-type]
    trigger_ids: list[TriggerId]
    anti_pattern_ids: list[AntiPatternId]
    formula_id: FormulaId
    hypothesis: str = Field(description="Гипотеза: чем этот хук может удерживать или терять зрителя, со ссылкой на приёмы из базы знаний")


class Paper(BaseModel):
    title: str
    authors: str
    year: int
    venue: str = Field(description="Журнал или конференция; для препринта — arXiv/SSRN")
    link: str = Field(description="DOI (https://doi.org/...) или URL страницы, найденной в поиске")
    study_type: Literal["field_experiment", "lab_experiment", "observational", "meta_analysis", "review", "preprint"]
    peer_reviewed: bool
    sample: str = Field(description="Объём и тип данных: сколько людей/видео/экспериментов")
    finding: str = Field(description="Главный результат одним-двумя предложениями, с цифрами из источника")
    trigger_ids: list[TriggerId] = Field(description="К каким приёмам базы знаний относится")
    evidence_level: Literal["A", "B", "C", "D"]


class LiteratureScan(BaseModel):
    summary: str = Field(description="Что нового в науке о внимании и удержании за период")
    papers: list[Paper]
