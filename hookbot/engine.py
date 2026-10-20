"""HookEngine: the four product features plus the learning loop, on the Claude API."""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
import pydantic

from . import prompts
from .core import knowledge, learning
from .core.learning import Calibration, LearningContext
from .core.memory import MemoryStore
from .schemas import ActionPlan, Evaluation, Generation, HookVariant, Playbook, Rewrite
from .core.video import Frame

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

T = TypeVar("T", bound=pydantic.BaseModel)


class EngineError(RuntimeError):
    """A failure the user should see as a friendly message."""


class RefusalError(EngineError):
    pass


@dataclass
class HookInput:
    text: str
    on_screen_text: str = ""
    visual: str = ""
    audio: str = ""
    niche: str = ""
    script: str = ""

    def to_prompt(self) -> str:
        parts = [f"Голос / главная фраза: {self.text.strip()}"]
        if self.on_screen_text:
            parts.append(f"Текст на экране: {self.on_screen_text.strip()}")
        if self.visual:
            parts.append(f"Визуал: {self.visual.strip()}")
        if self.audio:
            parts.append(f"Звук: {self.audio.strip()}")
        if self.niche:
            parts.append(f"Ниша: {self.niche.strip()}")
        block = prompts.wrap("hook", "\n".join(parts))
        if self.script:
            block += "\n\n" + prompts.wrap("script", self.script)
        return block


@dataclass
class EvaluationReport:
    hook_id: int | None
    evaluation: Evaluation
    criteria: dict[str, int]
    score: float | None
    predicted_hold: float | None
    calibration: Calibration
    video_facts: dict[str, Any] | None = None
    improved_ids: list[int] = field(default_factory=list)


@dataclass
class VariantsReport:
    result: Generation | Rewrite
    hook_ids: list[int]

    @property
    def variants(self) -> list[HookVariant]:
        return self.result.hooks if isinstance(self.result, Generation) else self.result.rewrites


@dataclass
class ResultOutcome:
    hook: dict[str, Any]
    metrics: dict[str, Any]
    predicted_hold: float | None
    calibration: Calibration
    n_results: int
    reflection_due: bool

    @property
    def prediction_error(self) -> float | None:
        if self.predicted_hold is None or self.metrics.get("hold_3s") is None:
            return None
        return round(self.metrics["hold_3s"] - self.predicted_hold, 1)


def _criteria(evaluation: Evaluation) -> dict[str, int]:
    return {
        cid: max(0, min(10, int(getattr(evaluation.scores, cid).score)))
        for cid in knowledge.criterion_ids()
    }


class HookEngine:
    def __init__(
        self,
        store: MemoryStore,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        model: str | None = None,
        effort: str | None = None,
        fallbacks: bool | None = None,
        max_tokens: int = 16000,
        reflect_every: int = 5,
    ):
        self.store = store
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model or os.getenv("HOOKBOT_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.getenv("HOOKBOT_EFFORT", DEFAULT_EFFORT)
        if fallbacks is None:
            fallbacks = os.getenv("HOOKBOT_FALLBACKS", "default") != "off"
        self.fallbacks = fallbacks
        self.max_tokens = max_tokens
        self.reflect_every = reflect_every

    # ------------------------------------------------------------------ #
    # Claude call
    # ------------------------------------------------------------------ #

    async def _ask(self, schema: type[T], content: list[dict[str, Any]]) -> T:
        params: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": prompts.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
            },
        )
        if self.fallbacks:
            params.update(betas=[FALLBACK_BETA], fallbacks="default")
        try:
            response = await self.client.beta.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise EngineError("Сервис перегружен, попробуйте через минуту.") from exc
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            raise EngineError("Не удалось получить ответ от модели. Попробуйте ещё раз.") from exc
        except anthropic.APIConnectionError as exc:
            raise EngineError("Нет связи с сервером модели. Попробуйте ещё раз.") from exc
        except TypeError as exc:
            # The SDK raises a plain TypeError, not an API error, when no credentials are configured.
            if "authentication" not in str(exc):
                raise
            raise EngineError("Не настроен доступ к Claude API: задайте ANTHROPIC_API_KEY.") from exc

        usage = response.usage
        log.info(
            "claude %s stop=%s in=%s cache_read=%s out=%s",
            schema.__name__, response.stop_reason, usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", None), usage.output_tokens,
        )
        if response.stop_reason == "refusal":
            raise RefusalError("Модель отказалась обрабатывать этот запрос. Переформулируйте текст.")
        if response.stop_reason == "max_tokens":
            raise EngineError("Ответ получился слишком длинным. Сократите сценарий и попробуйте снова.")

        # After a server-side fallback the final model's answer follows the last fallback block.
        blocks = list(response.content)
        last_fallback = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
        text = "".join(b.text for b in blocks[last_fallback + 1 :] if b.type == "text")
        try:
            return schema.model_validate_json(text)
        except pydantic.ValidationError as exc:
            log.error("Invalid %s output: %s", schema.__name__, exc)
            raise EngineError("Модель вернула некорректный ответ. Попробуйте ещё раз.") from exc

    def _content(self, workspace: str, task: str, payload: str, images: list[Frame] = ()) -> list[dict]:
        ctx = self.context(workspace)
        text = "\n\n".join(
            [ctx.to_prompt(), task, prompts.language_line(ctx.profile.get("language", "ru")), payload]
        )
        content: list[dict[str, Any]] = []
        for frame in images:
            content.append({"type": "text", "text": f"Кадр на {frame.t:.1f} с:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(frame.jpeg).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": text})
        return content

    def context(self, workspace: str) -> LearningContext:
        return learning.build_context(self.store, workspace)

    def _store_variants(
        self, workspace: str, variants: list[HookVariant], source: str, niche: str, parent_id: int | None
    ) -> list[int]:
        return [
            self.store.add_hook(
                workspace,
                v.spoken,
                source=source,
                on_screen_text=v.on_screen_text,
                visual=v.visual,
                niche=niche,
                triggers=v.trigger_ids,
                parent_id=parent_id,
                meta={"formula_id": v.formula_id, "why": v.why},
            )
            for v in variants
        ]

    # ------------------------------------------------------------------ #
    # Features
    # ------------------------------------------------------------------ #

    async def evaluate(
        self,
        workspace: str,
        hook: HookInput,
        *,
        frames: list[Frame] = (),
        video_facts: str = "",
        source: str = "evaluated",
        save: bool = True,
    ) -> EvaluationReport:
        """Score a hook against the rubric (feature 1)."""
        niche = hook.niche or self.store.get_profile(workspace)["niche"]
        payload = hook.to_prompt()
        if video_facts:
            payload += "\n\n" + prompts.wrap("video_facts", video_facts)
        evaluation = await self._ask(
            Evaluation, self._content(workspace, prompts.EVALUATE_TASK, payload, list(frames))
        )
        criteria = _criteria(evaluation)
        calibration = learning.calibrate(self.store, workspace)
        score = calibration.score(criteria)
        predicted = calibration.predict_hold(criteria)

        hook_id = None
        improved_ids: list[int] = []
        if save:
            hook_id = self.store.add_hook(
                workspace,
                hook.text,
                source=source,
                on_screen_text=hook.on_screen_text,
                visual=hook.visual,
                niche=niche,
                triggers=evaluation.trigger_ids,
                criteria=criteria,
                score=score,
                predicted_hold=predicted,
                meta={"anti_patterns": evaluation.anti_pattern_ids, "video": bool(frames)},
            )
            improved_ids = self._store_variants(workspace, evaluation.improved, "improved", niche, hook_id)
        return EvaluationReport(
            hook_id=hook_id,
            evaluation=evaluation,
            criteria=criteria,
            score=score,
            predicted_hold=predicted,
            calibration=calibration,
            video_facts={"text": video_facts} if video_facts else None,
            improved_ids=improved_ids,
        )

    async def generate(self, workspace: str, script: str, *, count: int = 8, niche: str = "") -> VariantsReport:
        """Write new hooks for a script (feature 2)."""
        niche = niche or self.store.get_profile(workspace)["niche"]
        payload = prompts.wrap("script", script)
        if niche:
            payload = f"Ниша: {niche}\n\n" + payload
        task = prompts.GENERATE_TASK.format(count=count)
        result = await self._ask(Generation, self._content(workspace, task, payload))
        ids = self._store_variants(workspace, result.hooks, "generated", niche, None)
        return VariantsReport(result=result, hook_ids=ids)

    async def rewrite(self, workspace: str, hook: HookInput, *, parent_id: int | None = None) -> VariantsReport:
        """Rewrite an existing hook to hold attention (feature 3)."""
        niche = hook.niche or self.store.get_profile(workspace)["niche"]
        result = await self._ask(Rewrite, self._content(workspace, prompts.REWRITE_TASK, hook.to_prompt()))
        if parent_id is None:
            parent_id = self.store.add_hook(
                workspace, hook.text, source="original", on_screen_text=hook.on_screen_text,
                visual=hook.visual, niche=niche,
            )
        ids = self._store_variants(workspace, result.rewrites, "rewritten", niche, parent_id)
        return VariantsReport(result=result, hook_ids=ids)

    async def plan_actions(self, workspace: str, hook: HookInput) -> ActionPlan:
        """Concrete sound / in-frame action / on-screen text / edit options (feature 4)."""
        return await self._ask(ActionPlan, self._content(workspace, prompts.ACTIONS_TASK, hook.to_prompt()))

    # ------------------------------------------------------------------ #
    # Learning loop
    # ------------------------------------------------------------------ #

    async def record_result(self, workspace: str, hook_id: int, *, notes: str = "", **metrics: Any) -> ResultOutcome:
        """Store real Instagram metrics for a published hook and update the model.

        Hooks that were generated (and never scored) are scored first, without
        showing the model the result, so they can join the training set.
        """
        hook = self.store.get_hook(hook_id, workspace)
        if hook is None:
            raise EngineError(f"Хук #{hook_id} не найден.")
        clean = self.store.normalize_metrics(**metrics)  # fail on bad input before spending an API call

        # Only results with a 3-second hold train the model. Score unscored hooks before
        # storing the result, so the hook's own outcome cannot leak into the prompt.
        if not hook["criteria"] and clean.get("hold_3s") is not None:
            try:
                report = await self.evaluate(
                    workspace,
                    HookInput(text=hook["text"], on_screen_text=hook["on_screen_text"], visual=hook["visual"],
                              niche=hook["niche"]),
                    save=False,
                )
            except EngineError:
                log.warning("Could not score hook #%s for training", hook_id)
            else:
                self.store.update_hook_analysis(
                    hook_id,
                    criteria=report.criteria,
                    triggers=sorted(set(hook["triggers"]) | set(report.evaluation.trigger_ids)),
                    score=report.score,
                    predicted_hold=None,  # predicting after the fact would not be an honest forecast
                )
        clean = self.store.add_result(hook_id, workspace, notes=notes, **metrics)
        calibration = learning.calibrate(self.store, workspace)
        return ResultOutcome(
            hook=self.store.get_hook(hook_id, workspace),
            metrics=clean,
            predicted_hold=hook["predicted_hold"],
            calibration=calibration,
            n_results=self.store.count_results(workspace),
            reflection_due=self.store.reflection_due(workspace, self.reflect_every),
        )

    async def reflect(self, workspace: str) -> Playbook | None:
        """Distill the account's results into a playbook that steers future prompts."""
        rows = self.store.training_rows(workspace)
        if len(rows) < 3:
            return None
        ctx = self.context(workspace)
        table = [
            {
                "hook": r["text"],
                "on_screen_text": r["on_screen_text"],
                "visual": r["visual"],
                "triggers": r["triggers"],
                "criteria": r["criteria"],
                "hold_3s": r["hold_3s"],
                "views": r["views"],
                "avg_watch_sec": r["avg_watch_sec"],
                "shares": r["shares"],
                "saves": r["saves"],
            }
            for r in sorted(rows, key=lambda r: r["hold_3s"], reverse=True)
        ]
        stats = {
            "calibration": ctx.calibration.to_dict(),
            "trigger_lift": ctx.lifts,
        }
        payload = (
            prompts.wrap("results", json.dumps(table, ensure_ascii=False, indent=1))
            + "\n\n"
            + prompts.wrap("stats", json.dumps(stats, ensure_ascii=False, indent=1))
        )
        playbook = await self._ask(Playbook, self._content(workspace, prompts.REFLECT_TASK, payload))
        self.store.save_insight(workspace, playbook.model_dump(), n_results=len(rows))
        return playbook
