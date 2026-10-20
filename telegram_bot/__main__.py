"""Run the bot: ``python -m telegram_bot`` (long polling)."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from hookbot.core.memory import MemoryStore
from hookbot.engine import HookEngine

from .config import Settings
from .handlers import router
from .scheduler import followup_loop
from .storage import BotStorage

COMMANDS = [
    BotCommand(command="score", description="Оценить хук"),
    BotCommand(command="generate", description="Хуки по сценарию"),
    BotCommand(command="rewrite", description="Переписать хук"),
    BotCommand(command="actions", description="Звук, действие, текст для первых 3 секунд"),
    BotCommand(command="result", description="Внести статистику опубликованного ролика"),
    BotCommand(command="stats", description="Чему бот научился"),
    BotCommand(command="playbook", description="Плейбук хуков"),
    BotCommand(command="niche", description="Ниша и аудитория"),
    BotCommand(command="balance", description="Баланс и пополнение"),
    BotCommand(command="help", description="Справка"),
]


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    memory = MemoryStore(settings.db_path)
    botdb = BotStorage(memory)
    engine = HookEngine(memory, reflect_every=settings.reflect_every)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp["engine"] = engine
    dp["botdb"] = botdb
    dp["settings"] = settings
    dp["limiter"] = asyncio.Semaphore(settings.max_concurrent)
    dp.include_router(router)

    await bot.set_my_commands(COMMANDS)
    followups = asyncio.create_task(followup_loop(bot, memory, botdb, settings))
    try:
        await dp.start_polling(bot)
    finally:
        followups.cancel()
        memory.close()


if __name__ == "__main__":
    asyncio.run(main())
