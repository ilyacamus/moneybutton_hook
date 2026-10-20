"""Fresh research found by the robot's literature scans, gated by human approval.

Papers found on the web are candidates: the model can misread a source, so a
person approves them before they reach the evaluator's prompts.
"""

from __future__ import annotations

from typing import Any

from .memory import MemoryStore

SCAN_WS = "__literature__"
APPROVED_WS = "__literature_approved__"


def latest_scan(store: MemoryStore) -> dict[str, Any] | None:
    insight = store.latest_insight(SCAN_WS)
    return insight["content"] if insight else None


def approved(store: MemoryStore) -> list[dict[str, Any]]:
    insight = store.latest_insight(APPROVED_WS)
    return insight["content"]["papers"] if insight else []


def pending(store: MemoryStore) -> list[dict[str, Any]]:
    scan = latest_scan(store)
    if not scan:
        return []
    known = {p["link"] for p in approved(store)}
    return [p for p in scan["papers"] if p["link"] not in known]


def approve(store: MemoryStore, indexes: list[int]) -> list[dict[str, Any]]:
    """Approve papers by their 1-based position in ``pending``. Returns the newly approved."""
    candidates = pending(store)
    chosen = [candidates[i - 1] for i in indexes if 1 <= i <= len(candidates)]
    if chosen:
        papers = approved(store) + chosen
        store.save_insight(APPROVED_WS, {"papers": papers}, n_results=len(papers))
    return chosen


def prompt_block(store: MemoryStore, limit: int = 5) -> str:
    papers = sorted(approved(store), key=lambda p: p.get("year", 0), reverse=True)[:limit]
    if not papers:
        return ""
    lines = ["### Свежие исследования (найдены роботом, проверены человеком)"]
    for p in papers:
        lines.append(f"- {p['authors']} ({p['year']}), {p['venue']} [{p['evidence_level']}]: {p['finding']}")
    return "\n".join(lines)
