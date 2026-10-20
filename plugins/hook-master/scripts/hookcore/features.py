"""Objective hook features: measured by code, not judged by the model.

They complement the LLM rubric with variables anyone can re-measure, which
is what lets the corpus analysis test hypotheses instead of opinions.
"""

from __future__ import annotations

import re
from typing import Any

_FLAGS = re.IGNORECASE | re.UNICODE
SECOND_PERSON = re.compile(r"\b(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоих|вы|вас|вам|вами|ваш|ваша|ваше|ваши|you|your|you're|yours)\b", _FLAGS)
FIRST_PERSON = re.compile(r"\b(я|меня|мне|мной|мой|моя|моё|мои|мы|нас|нам|наш|наша|наши|i|i'm|me|my|we|our)\b", _FLAGS)
GREETING = re.compile(r"^\W*(привет|всем привет|здравствуйте|добрый (день|вечер)|хай|салют|hi|hello|hey)\b", _FLAGS)
STOP_WORD = re.compile(r"\b(стоп|подожди|остановись|замри|stop|wait)\b", _FLAGS)
NEGATIVE = re.compile(
    r"\b(ошиб\w*|никогда|нельзя|теря\w*|потер\w*|провал\w*|хуже|худш\w*|опасн\w*|вред\w*|ужас\w*|кошмар\w*|"
    r"перестан\w*|катастроф\w*|проблем\w*|страх\w*|обман\w*|развод\w*|слив\w*|убива\w*|"
    r"worst|never|mistakes?|lose|losing|lost|wrong|danger\w*|fail\w*|bad|terrible|scam\w*|stop)\b",
    _FLAGS,
)
NUMBER = re.compile(r"\d")
WORD = re.compile(r"[\w'’-]+", _FLAGS)

# name -> (kind, human label, which direction the folk theory says is better)
FEATURE_SPECS: dict[str, tuple[str, str, str | None]] = {
    "words": ("continuous", "Слов в первой фразе", None),
    "has_number": ("binary", "Есть цифра", "higher"),
    "has_question": ("binary", "Вопрос", None),
    "second_person": ("binary", "Обращение на «ты/вы»", "higher"),
    "first_person": ("binary", "Рассказ от первого лица", None),
    "negative_words": ("continuous", "Негативных слов", "higher"),
    "greeting_start": ("binary", "Начинается с приветствия", "lower"),
    "stop_word": ("binary", "«Стоп»/«подожди»", None),
    "first_sound_at": ("continuous", "Первый звук, с", "lower"),
    "cuts_3s": ("continuous", "Склеек в первые 3 с", "higher"),
    "face_first_second": ("binary", "Лицо в первую секунду", "higher"),
    "text_first_second": ("binary", "Текст на экране в первую секунду", "higher"),
    "motion_first_second": ("binary", "Движение в первую секунду", "higher"),
}


def text_features(text: str) -> dict[str, float]:
    text = (text or "").strip()
    words = WORD.findall(text)
    return {
        "words": float(len(words)),
        "has_number": float(bool(NUMBER.search(text))),
        "has_question": float("?" in text),
        "second_person": float(bool(SECOND_PERSON.search(text))),
        "first_person": float(bool(FIRST_PERSON.search(text))),
        "negative_words": float(len(NEGATIVE.findall(text))),
        "greeting_start": float(bool(GREETING.search(text))),
        "stop_word": float(bool(STOP_WORD.search(text))),
    }


def video_features(facts: dict[str, Any] | None) -> dict[str, float]:
    """From ``VideoProbe.facts()``: when the sound starts and how many cuts in 3 s."""
    if not facts:
        return {}
    out: dict[str, float] = {"cuts_3s": float(len(facts.get("cuts_in_first_3s") or []))}
    if facts.get("has_audio") and facts.get("first_sound_at_sec") is not None:
        out["first_sound_at"] = float(facts["first_sound_at_sec"])
    elif facts.get("has_audio") is not None:
        out["first_sound_at"] = 3.0  # silent (or no audio) for the whole hook window
    return out


def vision_features(flags: dict[str, Any] | None) -> dict[str, float]:
    """From the model's look at the first frames (face/text/motion in the first second)."""
    if not flags:
        return {}
    keys = ("face_first_second", "text_first_second", "motion_first_second")
    return {k: float(bool(flags[k])) for k in keys if flags.get(k) is not None}


def extract(text: str, facts: dict[str, Any] | None = None, flags: dict[str, Any] | None = None) -> dict[str, float]:
    return {**text_features(text), **video_features(facts), **vision_features(flags)}


# Which knowledge-base item each objective feature operationalizes, and whether
# "more" is what that item predicts. Lets the corpus test the literature directly.
FEATURE_LITERATURE: dict[str, tuple[str, str, str]] = {
    "has_number": ("trigger", "specificity", "higher"),
    "has_question": ("trigger", "question_hook", "higher"),
    "second_person": ("trigger", "identity_callout", "higher"),
    "negative_words": ("trigger", "loss_aversion", "higher"),
    "greeting_start": ("anti_pattern", "slow_intro", "lower"),
    "first_sound_at": ("anti_pattern", "silent_start", "lower"),
    "cuts_3s": ("anti_pattern", "static_frame", "higher"),
    "face_first_second": ("trigger", "face_emotion", "higher"),
    "motion_first_second": ("trigger", "motion_first", "higher"),
}
