"""Turn free-form user messages into hook inputs and metrics."""

from __future__ import annotations

import re

from hookbot.engine import HookInput

# Order matters inside each tuple only for readability; matching uses the longest alias first.
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "skip_rate": ("skip rate", "skip", "скип рейт", "скипрейт", "скип", "пролистали", "пролистывания",
                  "пролист", "свайпнули", "пропуски"),
    "hold_3s": ("удержание 3 с", "удержание 3с", "удержание", "hold", "досмотр 3с", "досмотр 3 с"),
    "views": ("просмотров", "просмотры", "просмотр", "views", "plays", "прос"),
    "avg_watch_sec": ("среднее время просмотра", "ср. время просмотра", "среднее время", "ср. время", "ср время",
                      "avg watch", "avg", "среднее", "ср."),
    "video_length_sec": ("длительность", "хронометраж", "длина", "length"),
    "likes": ("лайков", "лайки", "лайк", "likes"),
    "comments": ("комментариев", "комментарии", "комментов", "комменты", "comments"),
    "shares": ("репостов", "репосты", "отправок", "отправки", "отправили", "shares", "sends"),
    "saves": ("сохранений", "сохранения", "сохранили", "сохр", "saves"),
    "follows": ("подписок", "подписки", "подписались", "подписчиков", "follows"),
}

_NUMBER = r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*(%|k|к|тыс\.?|m|м|млн)?"
_GAP = r"[\s:=\-–—]*"
_ALIASES = sorted(
    ((alias, field) for field, aliases in METRIC_ALIASES.items() for alias in aliases),
    key=lambda item: len(item[0]),
    reverse=True,
)
_ALIAS_RE = "|".join(re.escape(a) for a, _ in _ALIASES)
_ALIAS_TO_FIELD = {alias: field for alias, field in _ALIASES}
_KEY_THEN_NUM = re.compile(rf"(?<![\w])({_ALIAS_RE}){_GAP}{_NUMBER}", re.IGNORECASE)
_NUM_THEN_KEY = re.compile(rf"{_NUMBER}\s*({_ALIAS_RE})(?![\w])", re.IGNORECASE)

MULTIPLIERS = {"k": 1_000, "к": 1_000, "тыс": 1_000, "тыс.": 1_000, "m": 1_000_000, "м": 1_000_000, "млн": 1_000_000}


def _to_number(raw: str, unit: str | None) -> float:
    value = float(re.sub(r"[ \u00a0\u202f]", "", raw).replace(",", "."))
    if unit:
        value *= MULTIPLIERS.get(unit.lower(), 1)
    return value


def parse_metrics(text: str) -> dict[str, float]:
    """Parse «skip 38%, 12к просмотров, ср. время 6,5» into metric fields.

    Unknown fragments are ignored; the first value found for a field wins.
    """
    found: dict[str, float] = {}
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for m in _KEY_THEN_NUM.finditer(text):
        field = _ALIAS_TO_FIELD[m.group(1).lower()]
        if field not in found and free(m.span()):
            found[field] = _to_number(m.group(2), m.group(3))
            taken.append(m.span())
    for m in _NUM_THEN_KEY.finditer(text):
        field = _ALIAS_TO_FIELD[m.group(3).lower()]
        if field not in found and free(m.span()):
            found[field] = _to_number(m.group(1), m.group(2))
            taken.append(m.span())
    return found


def parse_number(text: str) -> float | None:
    m = re.search(_NUMBER, text)
    return _to_number(m.group(1), m.group(2)) if m else None


def parse_result_command(args: str) -> tuple[int | None, dict[str, float]]:
    """``"17 skip 38 views 12k"`` -> (17, {...}); a leading ``#`` is allowed."""
    m = re.match(r"\s*#?(\d+)\b(.*)", args or "", re.DOTALL)
    if not m:
        return None, parse_metrics(args or "")
    return int(m.group(1)), parse_metrics(m.group(2))


_FIELD_PREFIXES = {
    "on_screen_text": ("текст на экране", "на экране", "текст", "надпись", "screen"),
    "visual": ("визуал", "кадр", "видео", "visual"),
    "audio": ("звук", "музыка", "audio"),
    "niche": ("ниша",),
    "text": ("хук", "голос", "фраза", "hook"),
}
_PREFIX_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(p) for ps in _FIELD_PREFIXES.values() for p in sorted(ps, key=len, reverse=True)) + r")\s*[:\-–—]\s*(.*)$",
    re.IGNORECASE,
)


def parse_hook_message(text: str) -> HookInput:
    """Accept either a bare hook or labelled lines like «Текст: …», «Визуал: …»."""
    fields: dict[str, list[str]] = {}
    current = "text"
    for line in text.strip().splitlines():
        m = _PREFIX_RE.match(line)
        if m:
            prefix = m.group(1).lower()
            current = next(f for f, ps in _FIELD_PREFIXES.items() if prefix in ps)
            line = m.group(2)
        if line.strip():
            fields.setdefault(current, []).append(line.strip())
    joined = {k: " ".join(v) for k, v in fields.items()}
    hook_text = joined.pop("text", "") or joined.get("on_screen_text", "")
    return HookInput(text=hook_text, **joined)
