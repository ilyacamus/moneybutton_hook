"""Reels from an outlier index (vidIQ outlier search for Instagram and TikTok).

Each reel found comes with its views, the creator's median views and a
description of the first 3 seconds (what is on screen, the on-screen text,
the sound, the format). The index only lists breakouts (≥3× the creator's
median), so these reels are stored with ``feed=False``: they never enter a
creator's baseline. The description stands in for the video in blind
analysis; it contains no views.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .corpus import Corpus, ReelRecord

PLATFORMS = {"instagram": ("instagram", "ig"), "tiktok": ("tiktok", "tt")}
_UNITS = {"K": 1e3, "M": 1e6, "B": 1e9, "": 1.0}
_STATS = re.compile(r"([\d.,]+)([KMB]?) views \(([\d.,]+)x their median of ([\d.,]+)([KMB]?)\)"
                    r"(?: · ([\d.,]+)([KMB]?) followers)?")
DESCRIPTION_FIELDS = (("hook_visual", "В кадре"), ("hook_text", "Текст на экране"), ("hook_audio", "Звук"),
                      ("style", "Формат"), ("template", "Шаблон"), ("pacing", "Темп"),
                      ("text_overlays", "Надписи"), ("audio_mix", "Звуковая дорожка"))


def load_text(path: str | Path) -> str:
    """Tool output as saved by Claude Code: plain text, or a JSON list of text blocks."""
    raw = Path(path).read_text(encoding="utf-8")
    if raw.lstrip().startswith("["):
        try:
            return "".join(b.get("text", "") for b in json.loads(raw) if isinstance(b, dict))
        except json.JSONDecodeError:
            pass
    return raw


def _num(value: str, unit: str) -> float:
    return float(value.replace(",", "")) * _UNITS[unit]


def parse(text: str, query: str = "") -> list[dict[str, Any]]:
    """Reels in an outlier-search answer (Markdown: ``## Instagram`` / ``## TikTok`` sections)."""
    out = []
    for platform, section in re.findall(r"## (Instagram|TikTok)\n(.*?)(?=\n## (?:Instagram|TikTok)\n|\Z)", text, re.S):
        for block in re.split(r"\n\*\*@", "\n" + section)[1:]:
            head = block.split("\n")[0]
            m = _STATS.search(block)
            if not m:
                continue
            ident = re.search(r"(?:reel|video|tiktok):(\S+)", block) or re.search(r"/video/(\d+)", block)

            def field(name: str) -> str:
                found = re.search(rf"^\s*\**{name}\**: (.*)$", block, re.M)
                return found.group(1).strip() if found else ""

            caption = re.search(r'— "(.*)"', head)
            duration = re.search(r"· (\d+)s\b", block)
            out.append({
                "platform": platform.lower(), "creator": head.split("**")[0].strip().lower(),
                "id": ident.group(1) if ident else "", "caption": caption.group(1) if caption else "",
                "views": _num(m.group(1), m.group(2)), "x_median": float(m.group(3).replace(",", "")),
                "median": _num(m.group(4), m.group(5)),
                "followers": _num(m.group(6), m.group(7)) if m.group(6) else None,
                "duration": float(duration.group(1)) if duration else None,
                "concept": field("reel_concept") or field("tiktok_concept") or field("video_concept") or field("concept"),
                "niche": field("niche"), "hook_visual": field("visual"), "hook_text": field("text"),
                "hook_audio": field("audio"), "style": field("style"), "intended_value": field("intended_value"),
                "template": field("template"), "pacing": field("pacing"), "text_overlays": field("text_overlays"),
                "visual_changes": field("visual_changes"), "audio_mix": field("audio_mix"), "sfx": field("sfx_present"),
                "region": field("culture_region"), "demographics": field("demographics"), "query": query,
            })
    return out


def dedupe(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        seen.setdefault((item["platform"], item["id"] or item["caption"][:60]), item)
    return list(seen.values())


def description(item: dict[str, Any]) -> str:
    """What a viewer sees and hears in the first seconds — no views, no multiples (blind analysis)."""
    return "\n".join(f"{label}: {item[key]}" for key, label in DESCRIPTION_FIELDS if item.get(key))


def url(item: dict[str, Any]) -> str:
    if not item["id"]:
        return ""
    if item["platform"] == "tiktok":
        return f"https://www.tiktok.com/@{item['creator']}/video/{item['id']}"
    return f"https://www.instagram.com/reel/{item['id']}/"


def to_record(item: dict[str, Any], niche: str, workspace: str = "") -> ReelRecord:
    source, prefix = PLATFORMS[item["platform"]]
    return ReelRecord(
        source=source, external_id=item["id"] or f"{item['creator']}:{item['caption'][:60]}", url=url(item),
        creator=f"{prefix}:{item['creator']}", niche=niche, caption=item["caption"],
        views=int(item["views"]), baseline_views=item["median"],
        followers=int(item["followers"]) if item.get("followers") else None,
        description=description(item), feed=False, workspace=workspace,
    )


def import_items(corpus: Corpus, items: Iterable[dict[str, Any]], niche: str, workspace: str = "") -> dict[str, int]:
    """Store reels from the index (metrics refresh, existing analyses kept) and label them."""
    items = dedupe(items)
    new = 0
    for item in items:
        rec = to_record(item, niche, workspace)
        new += corpus.find(rec.source, rec.external_id) is None
        corpus.upsert(rec)
    corpus.label_all()
    return {"found": len(items), "new": new, "creators": len({(i["platform"], i["creator"]) for i in items})}


def import_file(corpus: Corpus, path: str | Path, niche: str, query: str = "", workspace: str = "") -> dict[str, int]:
    return import_items(corpus, parse(load_text(path), query), niche, workspace)
