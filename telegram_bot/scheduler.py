"""Background loop that closes the learning loop on its own.

48 hours after a hook is scored, the bot asks the author for the real skip
rate. Those answers are the training data for the per-account calibration.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from hookbot.core import breakout
from hookbot.core.corpus import Corpus
from hookbot.core.memory import MemoryStore

from . import keyboards as kb
from . import texts
from .config import Settings
from .storage import BotStorage

log = logging.getLogger(__name__)


async def send_due_followups(bot: Bot, memory: MemoryStore, botdb: BotStorage, settings: Settings) -> int:
    sent = 0
    for followup in botdb.due_followups():
        hook_id = followup["hook_id"]
        botdb.close_followup(hook_id)
        hook = memory.get_hook(hook_id)
        if hook is None or memory.has_result(hook_id):
            continue
        try:
            await bot.send_message(
                followup["user_id"],
                texts.FOLLOWUP.format(hook_id=hook_id, text=html.escape(hook["text"][:200]), bonus=settings.result_bonus),
                reply_markup=kb.followup(hook_id),
            )
            sent += 1
        except TelegramAPIError as exc:  # blocked the bot, deleted account, ...
            log.info("Follow-up for hook %s not delivered: %s", hook_id, exc)
    return sent


def backup_database(memory: MemoryStore, keep: int, *, now: datetime | None = None) -> Path | None:
    """Today's copy of the database in ``backups/`` next to it; only the newest ``keep`` copies stay."""
    if memory.path == ":memory:" or keep <= 0:
        return None
    folder = Path(memory.path).parent / "backups"
    day = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    path = memory.backup(folder / f"hookbot-{day}.db")
    for old in sorted(folder.glob("hookbot-*.db"))[:-keep]:
        old.unlink()
    return path


def refresh_niche_models(memory: MemoryStore) -> list[str]:
    """Retrain each niche's breakout model that has new studied reels. Returns the niches retrained."""
    retrained = []
    for niche in Corpus(memory).niche_coverage():
        stored = memory.latest_insight(breakout.MODEL_WS + niche)
        model = breakout.build(memory, niche)
        if model is not None and not (stored and stored["content"]["n"] == model["n"]
                                      and (stored["content"].get("within") or {}).get("n")
                                      == (model.get("within") or {}).get("n")):
            breakout.save(memory, model)
            retrained.append(niche)
    return retrained


async def maintenance_loop(memory: MemoryStore, settings: Settings, interval: float = 24 * 3600) -> None:
    """Once a day: back up the database and retrain niche models that got new data."""
    while True:
        try:
            path = await asyncio.to_thread(backup_database, memory, settings.backup_keep)
            retrained = await asyncio.to_thread(refresh_niche_models, memory)
            log.info("Maintenance: backup %s, retrained niche models: %s", path, retrained or "none")
        except Exception:
            log.exception("Maintenance failed")
        await asyncio.sleep(interval)


async def followup_loop(bot: Bot, memory: MemoryStore, botdb: BotStorage, settings: Settings, interval: float = 600) -> None:
    while True:
        try:
            await send_due_followups(bot, memory, botdb, settings)
        except Exception:
            log.exception("Follow-up loop iteration failed")
        await asyncio.sleep(interval)
