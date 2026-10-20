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

from hookbot.core.instagram import InstagramGraph
from hookbot.core.memory import MemoryStore
from hookbot.engine import HookEngine
from hookbot.scout import Scout, ScoutConfig
from hookbot.transcribe import make_transcriber

from .config import Settings
from .handlers import router
from .scheduler import followup_loop
from .storage import BotStorage, workspace_of

COMMANDS = [
    BotCommand(command="score", description="Оценить хук"),
    BotCommand(command="generate", description="Хуки по сценарию"),
    BotCommand(command="rewrite", description="Переписать хук"),
    BotCommand(command="actions", description="Звук, действие, текст для первых 3 секунд"),
    BotCommand(command="result", description="Внести статистику опубликованного ролика"),
    BotCommand(command="myreel", description="Прислать свой ролик со статистикой (аудит канала)"),
    BotCommand(command="audience", description="Что удерживает вашу аудиторию"),
    BotCommand(command="gap", description="Ваша аудитория: что работает у других авторов"),
    BotCommand(command="science", description="Что реально работает: данные + исследования"),
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

    # The scout: the Instagram token belongs to the bot owner's account, so the
    # owner's workspace is the one whose own reels and audience it audits.
    config = ScoutConfig.from_env()
    ig = InstagramGraph.from_env()
    if ig and not config.own_workspace and settings.admin_ids:
        config.own_workspace = workspace_of(min(settings.admin_ids))

    async def notify_admins(text: str) -> None:
        for admin in settings.admin_ids:
            try:
                await bot.send_message(admin, "🛰 " + text)
            except Exception:  # noqa: BLE001 - a blocked admin chat must not stop the scout
                logging.getLogger(__name__).warning("Cannot notify admin %s", admin)

    scout = Scout(memory, engine, ig=ig, config=config, notify=notify_admins,
                  transcriber=make_transcriber() if os.getenv("SCOUT_ASR") == "1" else None)

    dp = Dispatcher(storage=MemoryStorage())
    dp["engine"] = engine
    dp["botdb"] = botdb
    dp["settings"] = settings
    dp["limiter"] = asyncio.Semaphore(settings.max_concurrent)
    dp["scout"] = scout
    dp.include_router(router)

    await bot.set_my_commands(COMMANDS)
    tasks = [asyncio.create_task(followup_loop(bot, memory, botdb, settings))]
    if os.getenv("SCOUT_ENABLED") == "1":
        tasks.append(asyncio.create_task(scout.run_forever()))
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        memory.close()


if __name__ == "__main__":
    asyncio.run(main())
