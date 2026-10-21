"""Drive the real aiogram dispatcher with fake Telegram and Claude backends."""

import asyncio
from dataclasses import replace
from datetime import datetime
from itertools import count

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update, User

from hookbot.core.memory import MemoryStore
from hookbot.engine import HookEngine
from telegram_bot import handlers
from telegram_bot.config import Pack, Settings, parse_packs
from telegram_bot.scheduler import send_due_followups
from telegram_bot.storage import BotStorage, workspace_of

USER_ID = 777


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.ids = count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=1, is_bot=True, first_name="HookMaster", username="hookmaster_bot")
        if name in ("SendMessage", "SendInvoice", "EditMessageText", "EditMessageReplyMarkup"):
            chat_id = getattr(method, "chat_id", None) or USER_ID
            return Message(
                message_id=next(self.ids), date=datetime.now(), chat=Chat(id=chat_id, type="private"),
                text=getattr(method, "text", None),
            ).as_(bot)
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def close(self):
        pass

    def texts(self):
        return [r.text for r in self.requests if type(r).__name__ in ("SendMessage", "EditMessageText")]

    def of(self, name):
        return [r for r in self.requests if type(r).__name__ == name]


@pytest.fixture
def env(tmp_path, client):
    memory = MemoryStore(tmp_path / "bot.db")
    botdb = BotStorage(memory)
    settings = Settings(bot_token="42:TEST", free_credits=2, packs=parse_packs("start:15:150"), followup_hours=0)
    engine = HookEngine(memory, client)
    session = MockSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp["engine"], dp["botdb"], dp["settings"], dp["limiter"] = engine, botdb, settings, asyncio.Semaphore(2)
    # Routers can only be attached once; detach after the test.
    dp.include_router(handlers.router)
    ids = count(1)

    user = User(id=USER_ID, is_bot=False, first_name="Илья")
    chat = Chat(id=USER_ID, type="private")

    async def text(value, **extra):
        msg = Message(message_id=next(ids), date=datetime.now(), chat=chat, from_user=user, text=value, **extra)
        await dp.feed_update(bot, Update(update_id=next(ids), message=msg))

    async def press(data):
        msg = Message(message_id=next(ids), date=datetime.now(), chat=chat, from_user=user, text="…")
        cq = CallbackQuery(id=str(next(ids)), from_user=user, chat_instance="ci", data=data, message=msg)
        await dp.feed_update(bot, Update(update_id=next(ids), callback_query=cq))

    async def raw(update_kwargs):
        await dp.feed_update(bot, Update(update_id=next(ids), **update_kwargs))

    yield type("Env", (), dict(text=text, press=press, raw=raw, session=session, botdb=botdb, memory=memory,
                              settings=settings, bot=bot, user=user, chat=chat, client=client, dp=dp))
    handlers.router._parent_router = None
    memory.close()




async def test_start_gives_free_credits(env):
    await env.text("/start")
    assert "2 бесплатных анализов" in env.session.texts()[-1]
    await env.text("/start")
    assert "С возвращением" in env.session.texts()[-1]
    assert env.botdb.credits(USER_ID) == 2


async def test_score_flow_charges_and_schedules_followup(env):
    await env.text("/start")
    await env.text("Хотите больше клиентов из таргета?")
    assert "Похоже на хук" in env.session.texts()[-1]
    await env.press("mode:score")
    report = env.session.texts()[-1]
    assert "Рабочий хук" in report and "/result 1" in report
    assert env.botdb.credits(USER_ID) == 1
    assert env.botdb.due_followups()[0]["hook_id"] == 1

    sent = await send_due_followups(env.bot, env.memory, env.botdb, env.settings)
    assert sent == 1 and "#1" in env.session.texts()[-1]
    assert env.botdb.due_followups() == []


async def test_result_command_trains_and_rewards(env):
    await env.text("/start")
    await env.text("/score Я потратил 47 000 на таргет")
    await env.text("/result 1 skip 35% просмотры 12к")
    out = env.session.texts()[-1]
    assert "Удержание 3 с: <b>65%</b>" in out and "+1 анализ" in out
    assert env.botdb.credits(USER_ID) == 2  # 2 free - 1 score + 1 bonus
    await env.text("/result 1 skip 30%")  # bonus only once per hook
    assert env.botdb.credits(USER_ID) == 2
    assert env.memory.count_results(workspace_of(USER_ID)) == 1


async def test_guided_result_flow(env):
    await env.text("/start")
    await env.text("/score Хук")
    await env.press("res:1")
    assert "Хук <code>#1</code>" in env.session.texts()[-1]
    await env.text("41")  # a bare number is the skip rate
    assert "59%" in env.session.texts()[-1]


async def test_no_credits_offers_packs(env):
    await env.text("/start")
    env.botdb.charge(USER_ID, 2, "test")
    await env.text("/generate Сценарий ролика про таргет")
    last = env.session.of("SendMessage")[-1]
    assert "Недостаточно анализов" in last.text
    assert last.reply_markup.inline_keyboard[0][0].callback_data == "buy:start"
    assert env.client.calls == []


async def test_engine_failure_refunds(env):
    await env.text("/start")
    env.client.stop_reason = "refusal"
    await env.text("/rewrite Хук")
    assert "не списан" in env.session.texts()[-1]
    assert env.botdb.credits(USER_ID) == 2


async def test_stars_payment(env):
    await env.text("/start")
    await env.press("buy:start")
    invoice = env.session.of("SendInvoice")[-1]
    assert invoice.currency == "XTR" and invoice.prices[0].amount == 150 and invoice.payload == "pack:start:15:150"

    await env.raw({"pre_checkout_query": PreCheckoutQuery(
        id="pc1", from_user=env.user, currency="XTR", total_amount=150, invoice_payload=invoice.payload)})
    assert env.session.of("AnswerPreCheckoutQuery")[-1].ok is True
    await env.raw({"pre_checkout_query": PreCheckoutQuery(
        id="pc2", from_user=env.user, currency="XTR", total_amount=1, invoice_payload=invoice.payload)})
    assert env.session.of("AnswerPreCheckoutQuery")[-1].ok is False

    payment = SuccessfulPayment(currency="XTR", total_amount=150, invoice_payload=invoice.payload,
                                telegram_payment_charge_id="charge-1", provider_payment_charge_id="")
    for _ in range(2):  # Telegram may redeliver: credit once
        await env.text(None, successful_payment=payment)
    assert env.botdb.credits(USER_ID) == 17


async def test_deletedata(env):
    await env.text("/start")
    await env.text("/score Хук")
    await env.press("del:yes")
    assert env.memory.recent_hooks(workspace_of(USER_ID)) == []


def test_pack_payload_roundtrip():
    pack = Pack("pro", 50, 400)
    assert Pack.from_payload(pack.payload) == pack
    assert Pack.from_payload("garbage") is None


def test_split_message_respects_limit():
    text = "\n".join(f"<b>строка {i}</b>" for i in range(1000))
    chunks = handlers.split_message(text, limit=500)
    assert all(len(c) <= 500 for c in chunks) and "\n".join(chunks) == text


async def test_channel_commands_answer(env):
    await env.text("/start")
    await env.text("/audience")
    assert "Аудита канала ещё не было" in env.session.texts()[-1]
    await env.text("/gap")
    assert "запустите /target" in env.session.texts()[-1]
    await env.text("/target")  # not an admin: explains how it works
    assert "Целевая аудитория" in env.session.texts()[-1]
    await env.text("/science")
    assert "Корпус залетевших" in env.session.texts()[-1]
    await env.text("/watch @Coach_A")
    assert "@coach_a" in env.session.texts()[-1]
    await env.text("/validate")
    assert "доверять" in env.session.texts()[-1]
    await env.text("/myreel")
    assert "skip" in env.session.texts()[-1]


def private(env, owners):
    """Switch the running bot to private mode for these owners."""
    env.settings = replace(env.settings, private=True, admin_ids=frozenset(owners))
    env.dp["settings"] = env.settings


async def test_private_bot_refuses_strangers(env):
    private(env, {USER_ID + 1})
    await env.text("/start")
    await env.text("/score Хотите больше клиентов?")
    assert env.session.texts() == [f"🔒 Это личный бот, он закрыт для других пользователей. "
                                   f"Ваш Telegram id: <code>{USER_ID}</code>"] * 2
    assert env.botdb.get_user(USER_ID) is None and env.client.calls == []  # never stored, never analysed
    await env.press("mode:score")
    answer = env.session.of("AnswerCallbackQuery")[-1]
    assert answer.show_alert and "закрыт" in answer.text


async def test_private_bot_serves_its_owner(env):
    private(env, {USER_ID})
    env.botdb.apply_owner_niche(env.settings.admin_ids, "Крипта и трейдинг")
    await env.text("/start")
    welcome = env.session.texts()[-1]
    assert "Личный HookMaster" in welcome and "Крипта и трейдинг" in welcome and "модель на" in welcome
    await env.text("/balance")
    assert "без ограничений" in env.session.texts()[-1] and env.session.of("SendInvoice") == []
    await env.text("/score Хотите больше клиентов из таргета?")
    report = env.session.texts()[-1]
    assert "Рабочий хук" in report and "Потенциал в нише «Крипта и трейдинг»" in report
    assert env.botdb.credits(USER_ID) == 2  # the owner is never charged


def test_owner_niche_and_private_settings(monkeypatch, tmp_path):
    from telegram_bot.__main__ import commands

    botdb = BotStorage(MemoryStore(tmp_path / "o.db"))
    botdb.memory.update_profile(workspace_of(2), niche="фитнес")
    assert botdb.apply_owner_niche({1, 2}, "Крипта и трейдинг") == [1]  # a chosen niche is kept
    assert botdb.memory.get_profile(workspace_of(2))["niche"] == "фитнес"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "42:TEST")
    monkeypatch.setenv("BOT_PRIVATE", "1")
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    with pytest.raises(SystemExit, match="ADMIN_IDS"):
        Settings.from_env()
    monkeypatch.setenv("ADMIN_IDS", "123")
    settings = Settings.from_env()
    assert settings.private and settings.allowed(123) and not settings.allowed(5)
    assert "balance" not in {c.command for c in commands(settings)}


async def test_owner_works_in_the_channel_workspace(env):
    from telegram_bot.storage import set_workspace_aliases

    private(env, {USER_ID})
    env.memory.update_profile("kanyuka", niche="крипто-трейдинг")
    set_workspace_aliases({USER_ID: "kanyuka"})
    try:
        await env.text("/start")
        assert "Крипта" in env.session.texts()[-1] or "крипто" in env.session.texts()[-1]
        await env.text("/score Хотите больше клиентов из таргета?")
        assert "Хотите больше клиентов из таргета?" in {h["text"] for h in env.memory.recent_hooks("kanyuka", limit=20)}
        assert env.memory.recent_hooks(f"tg:{USER_ID}") == []
    finally:
        set_workspace_aliases({})
    assert workspace_of(USER_ID) == f"tg:{USER_ID}"


def test_daily_backups_and_model_refresh(tmp_path):
    from datetime import datetime, timezone

    from hookbot.core.corpus import Corpus
    from telegram_bot.scheduler import backup_database, refresh_niche_models
    from test_niches import synthetic_pack

    memory = MemoryStore(tmp_path / "bot.db")
    for day in (1, 2, 3):
        backup_database(memory, keep=2, now=datetime(2026, 10, day, tzinfo=timezone.utc))
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == ["hookbot-20261002.db", "hookbot-20261003.db"]
    assert MemoryStore(tmp_path / "backups" / "hookbot-20261003.db").get_profile("x")["niche"] == ""
    assert refresh_niche_models(memory) == []  # nothing studied yet
    Corpus(memory).import_pack(synthetic_pack())
    assert refresh_niche_models(memory) == ["crypto_trading"]
    assert refresh_niche_models(memory) == []  # no new reels: the model is kept
