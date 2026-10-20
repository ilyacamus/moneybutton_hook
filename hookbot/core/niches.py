"""Niche taxonomy: which niche a client's channel belongs to.

The knowledge base is collected per niche (each niche has its own corpus of
reels and its own breakout model, see ``breakout``). A client's channel is
only used to find its niche: the bio and the recent captions are matched
against each niche's keywords (``data/niches.json``), and the niche whose
topic covers the most of the channel wins.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Iterable

from .knowledge import DATA_DIR

BIO_WEIGHT = 2.0  # the bio states what the channel is about; one caption may be off-topic
MAX_RESULTS = 5
_LETTER = "0-9a-zа-я"


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


@lru_cache(maxsize=None)
def all_niches() -> tuple[dict[str, Any], ...]:
    with open(DATA_DIR / "niches.json", encoding="utf-8") as f:
        return tuple(json.load(f))


def ids() -> list[str]:
    return [n["id"] for n in all_niches()]


def get(niche_id: str) -> dict[str, Any] | None:
    return next((n for n in all_niches() if n["id"] == niche_id), None)


def name(niche_id: str) -> str:
    niche = get(niche_id)
    return niche["name"] if niche else niche_id


def keywords(niche_id: str) -> tuple[str, ...]:
    niche = get(niche_id)
    return tuple(niche["keywords"]) if niche else ()


def queries(niche_id: str) -> list[str]:
    niche = get(niche_id)
    return list(niche["queries"]) if niche else []


@lru_cache(maxsize=None)
def _pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    """Stems match at a word start ("крипт" → "криптой"); words of ≤3 letters must match whole."""
    parts = []
    for w in sorted({_norm(w) for w in words}, key=len, reverse=True):
        tail = f"(?![{_LETTER}])" if len(w) <= 3 else ""
        parts.append(f"(?<![{_LETTER}]){re.escape(w)}{tail}")
    return re.compile("|".join(parts) or "(?!)")


def matches(text: str, words: Iterable[str]) -> list[str]:
    """Distinct keywords found in a text."""
    return sorted(set(_pattern(tuple(words)).findall(_norm(text))))


def on_topic(text: str, niche_id: str) -> bool:
    return bool(text) and bool(_pattern(keywords(niche_id)).search(_norm(text)))


def classify(texts: Iterable[str], bio: str = "") -> dict[str, Any]:
    """Rank niches by the share of the channel (bio counts double) that is on their topic."""
    units = [(BIO_WEIGHT, bio)] if bio.strip() else []
    units += [(1.0, t) for t in texts if t and t.strip()]
    total = sum(w for w, _ in units)
    ranked = []
    for niche in all_niches():
        found: set[str] = set()
        covered = 0.0
        for weight, text in units:
            hit = matches(text, niche["keywords"])
            if hit:
                covered += weight
                found.update(hit)
        if covered:
            ranked.append({"id": niche["id"], "name": niche["name"], "share": round(covered / total, 3),
                           "keywords": sorted(found)[:10]})
    ranked.sort(key=lambda r: (r["share"], len(r["keywords"])), reverse=True)
    best = ranked[0] if ranked else None
    second = ranked[1]["share"] if len(ranked) > 1 else 0.0
    if best and best["share"] >= 0.25 and best["share"] >= 1.5 * second:
        confidence = "высокая"
    elif best and best["share"] >= 0.1:
        confidence = "средняя"
    else:
        confidence = "низкая"
    return {"best": best["id"] if best else None, "confidence": confidence, "n_texts": len(units),
            "niches": ranked[:MAX_RESULTS]}


def resolve(text: str) -> str | None:
    """Taxonomy id for a free-text niche ("крипто-трейдинг: индикаторы…"), or None."""
    value = _norm(text).strip()
    if not value:
        return None
    for niche in all_niches():
        if value in (niche["id"], _norm(niche["name"])):
            return niche["id"]
    return classify([value])["best"]


def canonical(tag: str) -> str:
    """Taxonomy id for a niche tag when it resolves to one; internal tags ("audience:…") and unknown niches stay as they are."""
    if not tag or re.match(r"^[a-z_]+:", tag):
        return tag or ""
    return resolve(tag) or tag.strip()


def detect(source: Any, username: str, limit: int = 12) -> dict[str, Any]:
    """Niche of an Instagram account from its public bio and recent captions (and transcripts if available)."""
    profile, media = source.discover(username, limit=limit)
    if not profile.get("biography") and hasattr(source, "profile"):
        profile = {**profile, **source.profile(username)}
    texts = [m.get("caption") or "" for m in media] + [m.get("transcript") or "" for m in media]
    result = classify(texts, bio=profile.get("biography") or "")
    return {"username": profile.get("username") or username.lstrip("@").lower(),
            "followers": profile.get("followers_count"), "media": len(media), **result}
