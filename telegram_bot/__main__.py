"""Run the bot: ``python -m telegram_bot`` (long polling); ``--check`` verifies the setup and exits."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

import anthropic

from hookbot.core import breakout, instagram, niches
from hookbot.core.memory import MemoryStore
from hookbot.engine import DEFAULT_MODEL, HookEngine
from hookbot.scout import Scout, ScoutConfig
from hookbot.transcribe import make_transcriber

from .config import Settings
from .handlers import router
from .scheduler import followup_loop, maintenance_loop
from .storage import BotStorage, set_workspace_aliases, workspace_of

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
PRIVATE_HIDDEN = {"balance"}  # no payments in private mode


def commands(settings: Settings) -> list[BotCommand]:
    return [c for c in COMMANDS if not (settings.private and c.command in PRIVATE_HIDDEN)]


async def check_claude(step) -> None:
    """One tiny real request: catches a wrong key, an empty balance or an unsupported region before users do."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        step(False, "Claude API: ключ ANTHROPIC_API_KEY не задан (console.anthropic.com → API keys)")
        return
    model = os.getenv("HOOKBOT_MODEL", DEFAULT_MODEL)
    try:
        async with anthropic.AsyncAnthropic() as client:
            await client.messages.create(model=model, max_tokens=1, messages=[{"role": "user", "content": "ping"}])
        step(True, f"Claude API: ключ работает, модель {model}")
    except anthropic.AuthenticationError:
        step(False, "Claude API: ключ не принят — создайте новый в console.anthropic.com → API keys")
    except anthropic.PermissionDeniedError as exc:
        step(False, f"Claude API: доступ запрещён ({exc.message}). Если это регион — запускайте бота на сервере "
                    "в поддерживаемой стране (docs/TELEGRAM_PRIVATE.md)")
    except anthropic.APIStatusError as exc:
        step(False, f"Claude API: ошибка {exc.status_code} — {exc.message} (баланс: console.anthropic.com → Billing)")
    except anthropic.APIConnectionError:
        step(False, "Claude API: нет соединения с api.anthropic.com")


async def check(settings: Settings, memory: MemoryStore) -> int:
    """Pre-flight: Telegram token, Claude key, owners, niche and its model. Prints each step; 0 when all pass."""
    ok = True

    def step(passed: bool, text: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(("✅ " if passed else "❌ ") + text)

    bot = Bot(settings.bot_token)
    try:
        me = await bot.get_me()
        step(True, f"Telegram: бот @{me.username}")
    except TelegramUnauthorizedError:
        step(False, "Telegram: токен не принят — возьмите токен заново у @BotFather (/mybots → API Token)")
    except TelegramNetworkError:
        step(False, "Telegram: нет связи с api.telegram.org — проверьте интернет на этом компьютере")
    except Exception as exc:  # noqa: BLE001 - report any other failure of the token check
        step(False, f"Telegram: {exc}")
    finally:
        await bot.session.close()
    await check_claude(step)
    mode = "личный: отвечает только " if settings.private else "открытый; админы: "
    step(bool(settings.admin_ids), "Доступ: " + mode + (", ".join(map(str, sorted(settings.admin_ids))) or "никто"))
    for admin in sorted(settings.admin_ids):
        workspace = workspace_of(admin)
        own = memory.conn.execute("SELECT COUNT(*) FROM reels WHERE workspace = ?", (workspace,)).fetchone()[0]
        results = memory.count_results(workspace)
        step(True, f"Данные владельца {admin} ({workspace}): своих роликов {own}, результатов {results}"
             + ("" if own or results else " — бот начнёт с нуля; OWNER_WORKSPACE подключает уже собранный канал"))
        niche = memory.get_profile(workspace)["niche"] or settings.owner_niche
        model = breakout.latest(memory, niches.canonical(niche)) if niche else None
        if not niche:
            step(False, f"Ниша владельца {admin}: не задана (OWNER_NICHE или /niche в боте)")
        elif model:
            step(True, f"Ниша владельца {admin}: {niche} — модель на {model['n']} роликах, качество {model['quality']}")
        else:
            step(True, f"Ниша владельца {admin}: {niche} — модели ниши пока нет, оценки по рубрике и науке")
    print("Готово к запуску: python -m telegram_bot" if ok else "Исправьте пункты с ❌ и запустите проверку снова.")
    return 0 if ok else 1


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if settings.owner_workspace:
        set_workspace_aliases({admin: settings.owner_workspace for admin in settings.admin_ids})
    memory = MemoryStore(settings.db_path)
    if "--check" in sys.argv[1:]:
        code = await check(settings, memory)
        memory.close()
        raise SystemExit(code)
    botdb = BotStorage(memory)
    botdb.apply_owner_niche(settings.admin_ids, settings.owner_niche)
    engine = HookEngine(memory, reflect_every=settings.reflect_every)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # The scout: the Instagram token belongs to the bot owner's account, so the
    # owner's workspace is the one whose own reels and audience it audits.
    config = ScoutConfig.from_env()
    ig = instagram.from_env()
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

    await bot.set_my_commands(commands(settings))
    if settings.private:
        logging.getLogger(__name__).info("Private mode: only %s", sorted(settings.admin_ids))
    tasks = [asyncio.create_task(followup_loop(bot, memory, botdb, settings)),
             asyncio.create_task(maintenance_loop(memory, settings))]
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
