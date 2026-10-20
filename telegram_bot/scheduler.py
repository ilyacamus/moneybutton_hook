"""Background loop that closes the learning loop on its own.

48 hours after a hook is scored, the bot asks the author for the real skip
rate. Those answers are the training data for the per-account calibration.
"""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

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


async def followup_loop(bot: Bot, memory: MemoryStore, botdb: BotStorage, settings: Settings, interval: float = 600) -> None:
    while True:
        try:
            await send_due_followups(bot, memory, botdb, settings)
        except Exception:
            log.exception("Follow-up loop iteration failed")
        await asyncio.sleep(interval)
