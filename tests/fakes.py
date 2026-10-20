"""Offline stand-in for ``anthropic.AsyncAnthropic`` with canned structured outputs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from hookbot.core import knowledge

VARIANT = {
    "spoken": "Я потратил 47 000 ₽ на таргет, чтобы вам не пришлось",
    "on_screen_text": "47 000 ₽ в минус",
    "visual": "Автор бросает пачку чеков в камеру",
    "formula_id": "did_it_for_you",
    "trigger_ids": ["authority_proof", "specificity"],
    "why": "Цифра и чужой опыт снимают риск для зрителя",
}


def evaluation(score: int = 6) -> dict[str, Any]:
    return {
        "verdict": "Есть цифра, но нет визуального стоппера",
        "scores": {cid: {"score": score, "reason": "ок"} for cid in knowledge.criterion_ids()},
        "trigger_ids": ["specificity", "curiosity_gap"],
        "anti_pattern_ids": ["static_frame"],
        "first_second": "Говорящая голова, нейтральное лицо",
        "swipe_risk": "На 1 секунде — нет движения",
        "strengths": ["Конкретная сумма"],
        "weaknesses": ["Статичный кадр"],
        "improved": [VARIANT, VARIANT, VARIANT],
    }


CANNED: dict[str, dict[str, Any]] = {
    "Evaluation": evaluation(),
    "Generation": {
        "core_promise": "Как не слить бюджет на таргет",
        "audience": "Владельцы малого бизнеса",
        "hooks": [VARIANT] * 8,
        "top_pick": 0,
        "top_pick_reason": "Самая конкретная выгода",
    },
    "Rewrite": {
        "diagnosis": "Общий вопрос без напряжения",
        "keep": "Тему таргета",
        "rewrites": [VARIANT] * 5,
        "top_pick": 1,
        "top_pick_reason": "Сильнее любопытство",
    },
    "ActionPlan": {
        "summary": "Добавить удар и цифру в первую секунду",
        "sound": [{"action_id": "s_impact", "instruction": "Удар на броске", "timing": "0.0–0.3 с", "why": "стоппер"}],
        "action": [{"action_id": "a_object_to_lens", "instruction": "Бросить чеки", "timing": "0.0–0.7 с", "why": "движение"}],
        "text": [{"action_id": "t_big_number", "text": "−47 000 ₽", "placement": "верхняя треть, крупно",
                  "timing": "0.0–1.5 с", "why": "конкретика"}],
        "edit": [{"action_id": "e_punch_zoom", "instruction": "Наезд на слове «таргет»", "timing": "1.0 с", "why": "акцент"}],
        "timeline": [
            {"window": "0.0–0.5 с", "visual": "бросок", "audio": "удар", "on_screen_text": "−47 000 ₽"},
            {"window": "0.5–1.5 с", "visual": "лицо", "audio": "голос", "on_screen_text": "−47 000 ₽"},
            {"window": "1.5–3.0 с", "visual": "экран кабинета", "audio": "голос", "on_screen_text": "3 ошибки"},
        ],
    },
    "ReelAnalysis": {
        "spoken_hook": "Я потратил 47 000 ₽ на таргет", "spoken_source": "caption",
        "on_screen_text": "−47 000 ₽", "visual": "Автор бросает чеки в камеру", "format": "talking_head",
        "face_first_second": True, "text_first_second": True, "motion_first_second": True,
        "scores": {cid: {"score": 7, "reason": "ок"} for cid in knowledge.criterion_ids()},
        "trigger_ids": ["specificity", "face_emotion"], "anti_pattern_ids": [], "formula_id": "did_it_for_you",
        "hypothesis": "Цифра и движение в первую секунду",
    },
    "LiteratureScan": {
        "summary": "Новая работа о склейках",
        "papers": [{
            "title": "The dynamic effects of visual complexity and scene cuts on viewer attention",
            "authors": "Wals et al.", "year": 2026, "venue": "JAMS", "link": "https://doi.org/10.1007/s11747-025-01137-x",
            "study_type": "observational", "peer_reviewed": True, "sample": "2 520 просмотров",
            "finding": "Склейки повышают синхронность внимания", "trigger_ids": ["motion_first"], "evidence_level": "B",
        }],
    },
    "Playbook": {
        "summary": "Аудитория держится на цифрах",
        "do_more": [{"rule": "Ставь сумму в первый кадр", "evidence": "+12 п.п.", "confidence": "medium"}],
        "do_less": [{"rule": "Не начинай с вопроса", "evidence": "−9 п.п.", "confidence": "low"}],
        "experiments": ["Сумма в тексте vs в голосе"],
    },
}


class FakeMessages:
    def __init__(self, client: "FakeClient"):
        self.client = client

    async def create(self, **params: Any) -> SimpleNamespace:
        self.client.calls.append(params)
        name = params["output_config"]["format"]["schema"]["title"]
        payload = self.client.overrides.get(name, CANNED.get(name))
        if self.client.responder is not None:
            payload = self.client.responder(name, params) or payload
        stop_reason = self.client.stop_reason
        if self.client.pause_first and len(self.client.calls) == 1:
            stop_reason, payload = "pause_turn", {}
        return SimpleNamespace(
            stop_reason=stop_reason,
            usage=SimpleNamespace(input_tokens=100, output_tokens=200, cache_read_input_tokens=0),
            content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        )


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.overrides: dict[str, Any] = {}
        self.stop_reason = "end_turn"
        self.pause_first = False
        self.responder = None  # optional (schema name, params) -> payload
        self.beta = SimpleNamespace(messages=FakeMessages(self))
