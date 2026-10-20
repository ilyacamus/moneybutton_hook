"""Telegram handlers: the four features, the learning loop and Stars payments."""

from __future__ import annotations

import asyncio
import html
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery, User
from aiogram.utils.chat_action import ChatActionSender

from hookbot import render
from hookbot.core import audience, breakout, corpus, instagram, learning, literature, niches, target, validation, video
from hookbot.core.memory import METRIC_FIELDS
from hookbot.engine import EngineError, HookEngine, HookInput

from . import keyboards as kb
from . import texts
from .config import COSTS, Pack, Settings
from .parsing import parse_hook_message, parse_metrics, parse_number, parse_result_command
from .storage import BotStorage, workspace_of

log = logging.getLogger(__name__)
router = Router()

MAX_MESSAGE = 4000
MAX_VIDEO_BYTES = 20 * 1024 * 1024  # Bot API download limit
MAX_INPUT_CHARS = 8000
T = TypeVar("T")


class Flow(StatesGroup):
    score = State()
    generate = State()
    rewrite = State()
    actions = State()
    result = State()
    niche = State()
    ref = State()
    myreel = State()


class TouchUserMiddleware(BaseMiddleware):
    """Registers every user (with free credits) and refreshes last_seen."""

    async def __call__(self, handler, event, data):
        user: User | None = data.get("event_from_user")
        if user is not None:
            botdb: BotStorage = data["botdb"]
            settings: Settings = data["settings"]
            _, data["new_user"] = botdb.touch_user(
                user.id, user.username or "", user.first_name or "", settings.free_credits
            )
        return await handler(event, data)


router.message.outer_middleware(TouchUserMiddleware())
router.callback_query.outer_middleware(TouchUserMiddleware())


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line boundaries; every line of our HTML closes its own tags."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single line
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return chunks


@dataclass
class Ctx:
    bot: Bot
    chat_id: int
    user_id: int
    engine: HookEngine
    storage: BotStorage
    settings: Settings
    limiter: asyncio.Semaphore
    scout: Any = None

    @property
    def workspace(self) -> str:
        return workspace_of(self.user_id)

    @property
    def unlimited(self) -> bool:
        return self.user_id in self.settings.admin_ids

    async def send(self, text: str, reply_markup=None) -> None:
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            await self.bot.send_message(
                self.chat_id, chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None
            )

    async def paid(self, op: str, work: Callable[[], Awaitable[T]]) -> T | None:
        """Charge credits, run ``work`` with a typing indicator, refund on failure."""
        cost = COSTS[op]
        if not self.storage.charge(self.user_id, cost, op, unlimited=self.unlimited):
            await self.send(
                texts.NO_CREDITS.format(cost=cost, credits=self.storage.credits(self.user_id)),
                reply_markup=kb.packs(self.settings.packs),
            )
            return None
        status = await self.bot.send_message(self.chat_id, "⏳ Анализирую… обычно это 20–60 секунд.")
        try:
            async with self.limiter, ChatActionSender.typing(bot=self.bot, chat_id=self.chat_id):
                result = await work()
        except (EngineError, video.VideoError) as exc:
            self.storage.refund_charge(self.user_id, cost, op, unlimited=self.unlimited)
            await status.edit_text(f"😕 {html.escape(str(exc))}\nАнализ не списан.")
            return None
        except Exception:
            log.exception("Operation %s failed", op)
            self.storage.refund_charge(self.user_id, cost, op, unlimited=self.unlimited)
            await status.edit_text("😕 Что-то пошло не так. Анализ не списан, попробуйте ещё раз.")
            return None
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        return result


def ctx_for(event: Message | CallbackQuery, bot: Bot, **data: Any) -> Ctx:
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    return Ctx(
        bot=bot,
        chat_id=chat_id,
        user_id=event.from_user.id,
        engine=data["engine"],
        storage=data["botdb"],
        settings=data["settings"],
        limiter=data["limiter"],
        scout=data.get("scout"),
    )


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


async def op_score(ctx: Ctx, hook: HookInput, *, frames=(), facts: str = "", source: str = "evaluated") -> None:
    report = await ctx.paid(
        "video" if frames else "score",
        lambda: ctx.engine.evaluate(ctx.workspace, hook, frames=frames, video_facts=facts, source=source),
    )
    if report is None:
        return
    text = render.render_evaluation(report)
    text += (
        f"\n\n💡 Опубликуете — пришлите статистику: <code>/result {report.hook_id} skip 38% просмотры 12к</code>. "
        "Так я учусь на вашей аудитории."
    )
    await ctx.send(text, reply_markup=kb.after_evaluation(report.hook_id))
    ctx.storage.schedule_followup(report.hook_id, ctx.user_id, ctx.settings.followup_hours)


async def op_generate(ctx: Ctx, script: str) -> None:
    report = await ctx.paid("generate", lambda: ctx.engine.generate(ctx.workspace, script))
    if report is None:
        return
    best = report.hook_ids[min(report.result.top_pick, len(report.hook_ids) - 1)]
    await ctx.send(render.render_variants(report), reply_markup=kb.after_variants(best))


async def op_rewrite(ctx: Ctx, hook: HookInput, parent_id: int | None = None) -> None:
    report = await ctx.paid("rewrite", lambda: ctx.engine.rewrite(ctx.workspace, hook, parent_id=parent_id))
    if report is None:
        return
    best = report.hook_ids[min(report.result.top_pick, len(report.hook_ids) - 1)]
    await ctx.send(render.render_variants(report), reply_markup=kb.after_variants(best))


async def op_actions(ctx: Ctx, hook: HookInput) -> None:
    plan = await ctx.paid("actions", lambda: ctx.engine.plan_actions(ctx.workspace, hook))
    if plan is not None:
        await ctx.send(render.render_actions(plan))


async def op_result(ctx: Ctx, hook_id: int, metrics: dict[str, float]) -> None:
    metrics = {k: v for k, v in metrics.items() if k in METRIC_FIELDS}
    hook = ctx.storage.memory.get_hook(hook_id, ctx.workspace)
    if hook is None:
        await ctx.send(f"Хук <code>#{hook_id}</code> не найден среди ваших.")
        return
    try:
        async with ctx.limiter, ChatActionSender.typing(bot=ctx.bot, chat_id=ctx.chat_id):
            outcome = await ctx.engine.record_result(ctx.workspace, hook_id, **metrics)
    except (EngineError, LookupError, ValueError) as exc:
        await ctx.send(f"😕 {html.escape(str(exc))}")
        return
    ctx.storage.close_followup(hook_id)
    text = render.render_result(outcome)
    if hook["source"] in learning.SCORED_SOURCES and ctx.storage.grant_result_bonus(
        ctx.user_id, hook_id, ctx.settings.result_bonus
    ):
        text += f"\n\n🎁 +{ctx.settings.result_bonus} анализ за результат. Баланс: {ctx.storage.credits(ctx.user_id)}"
    await ctx.send(text)
    if outcome.reflection_due:
        await op_learn(ctx, auto=True)


async def op_learn(ctx: Ctx, *, auto: bool = False) -> None:
    try:
        async with ctx.limiter, ChatActionSender.typing(bot=ctx.bot, chat_id=ctx.chat_id):
            playbook = await ctx.engine.reflect(ctx.workspace)
    except EngineError as exc:
        if not auto:
            await ctx.send(f"😕 {html.escape(str(exc))}")
        return
    if playbook is None:
        if not auto:
            await ctx.send(render.render_playbook(None))
        return
    head = "🧠 <b>Я переобучился на ваших новых результатах.</b>\n\n" if auto else ""
    await ctx.send(head + render.render_playbook(playbook.model_dump()))


async def _check_video(ctx: Ctx, message: Message) -> Any:
    media = message.video or message.video_note or message.document
    if media.file_size and media.file_size > MAX_VIDEO_BYTES:
        await ctx.send("Видео больше 20 МБ — Telegram не даёт ботам скачивать такие файлы. Обрежьте ролик до первых 5–10 секунд.")
        return None
    if not video.ffmpeg_available():
        await ctx.send("Разбор видео временно недоступен. Пришлите хук текстом.")
        return None
    return media


async def op_video(ctx: Ctx, message: Message) -> None:
    media = await _check_video(ctx, message)
    if media is None:
        return
    hook = parse_hook_message(message.caption) if message.caption else HookInput(text="")
    if not hook.text:
        hook.text = "[речь в начале не указана — оценка по кадрам]"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "video.mp4"
        try:
            await ctx.bot.download(media.file_id, destination=path)
            probe = await asyncio.to_thread(video.probe, str(path))
        except (video.VideoError, TelegramBadRequest) as exc:
            await ctx.send(f"Не получилось обработать видео: {html.escape(str(exc))}")
            return
    hook.visual = hook.visual or "см. кадры видео"
    await op_score(ctx, hook, frames=probe.frames, facts=probe.facts_text(), source="video")


async def _study_sent_video(ctx: Ctx, message: Message, op: str, **kwargs: Any) -> tuple[dict, dict] | None:
    """Download a sent reel and study it with the scout (blind analysis)."""
    media = await _check_video(ctx, message)
    if media is None or ctx.scout is None:
        return None
    caption = message.caption or ""

    async def work():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reel.mp4"
            await ctx.bot.download(media.file_id, destination=path)
            return await ctx.scout.study_file(str(path), workspace=ctx.workspace,
                                              niche=ctx.storage.memory.get_profile(ctx.workspace)["niche"],
                                              caption=caption, **kwargs)

    return await ctx.paid(op, work)


async def op_ref(ctx: Ctx, message: Message) -> None:
    """A viral reel the user found: study it and compare with the user's own hooks."""
    metrics = parse_metrics(message.caption or "")
    result = await _study_sent_video(ctx, message, "video", views=metrics.get("views"),
                                     typical_views=metrics.get("typical_views"))
    if result is None:
        return
    reel, data = result
    text = render.render_reel_analysis(data, reel)
    cal = learning.calibrate(ctx.storage.memory, ctx.workspace)
    own = [h["score"] for h in ctx.storage.memory.recent_hooks(ctx.workspace, limit=30)
           if h["score"] is not None and h["source"] in learning.SCORED_SOURCES]
    ref_score = cal.score(data["criteria"])
    if own and ref_score is not None:
        text += (f"\n\n📐 Балл этого ролика по вашей модели: {ref_score:.0f}; "
                 f"средний балл ваших хуков: {sum(own) / len(own):.0f} (n={len(own)}).")
    text += "\n\nРолик добавлен в корпус эталонов: он участвует в сравнении залетевших и обычных роликов."
    await ctx.send(text)


async def op_myreel(ctx: Ctx, message: Message) -> None:
    """One of the user's own published reels with its real skip rate: training data for the audit."""
    metrics = parse_metrics(message.caption or "")
    skip = metrics.get("skip_rate")
    if skip is None and metrics.get("hold_3s") is not None:
        skip = 100 - metrics["hold_3s"]
    if skip is None:
        await ctx.send("Нужен skip rate из статистики ролика. Пришлите видео ещё раз с подписью, например: "
                       "<code>skip 38% просмотры 12к</code>")
        return
    result = await _study_sent_video(ctx, message, "score", own=True, skip_rate=skip, views=metrics.get("views"))
    if result is None:
        return
    reel, data = result
    report = audience.latest_report(ctx.storage.memory, ctx.workspace) or {}
    n = report.get("n_studied", 0)
    text = render.render_reel_analysis(data, None) + f"\n\n✅ Ролик добавлен в аудит канала: {n} из {audience.MIN_REELS} нужных для статистики."
    if n >= audience.MIN_REELS:
        text += "\n\n" + render.render_audience(report)
    await ctx.send(text)


OPS = {
    "score": lambda ctx, text: op_score(ctx, parse_hook_message(text)),
    "generate": lambda ctx, text: op_generate(ctx, text),
    "rewrite": lambda ctx, text: op_rewrite(ctx, parse_hook_message(text)),
    "actions": lambda ctx, text: op_actions(ctx, parse_hook_message(text)),
}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, botdb: BotStorage, new_user: bool = False, **_: Any) -> None:
    await state.clear()
    credits = botdb.credits(message.from_user.id)
    text = texts.WELCOME.format(credits=credits) if new_user else texts.WELCOME_BACK.format(credits=credits)
    await message.answer(text, reply_markup=kb.main_menu())


@router.message(Command("help"))
@router.message(F.text == kb.BTN_HELP)
async def cmd_help(message: Message, **_: Any) -> None:
    await message.answer(texts.HELP, reply_markup=kb.main_menu())


@router.message(Command("terms"))
async def cmd_terms(message: Message, **_: Any) -> None:
    await message.answer(texts.TERMS)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, **_: Any) -> None:
    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=kb.main_menu())


MODE_BUTTONS = {
    kb.BTN_SCORE: (Flow.score, texts.ASK_SCORE),
    kb.BTN_GENERATE: (Flow.generate, texts.ASK_GENERATE),
    kb.BTN_REWRITE: (Flow.rewrite, texts.ASK_REWRITE),
    kb.BTN_ACTIONS: (Flow.actions, texts.ASK_ACTIONS),
}
MODE_COMMANDS = {"score": kb.BTN_SCORE, "generate": kb.BTN_GENERATE, "rewrite": kb.BTN_REWRITE, "actions": kb.BTN_ACTIONS}


@router.message(Command(*MODE_COMMANDS))
async def cmd_mode(message: Message, command: CommandObject, state: FSMContext, bot: Bot, **data: Any) -> None:
    if command.args:
        await state.clear()
        await OPS[command.command](ctx_for(message, bot, **data), command.args[:MAX_INPUT_CHARS])
        return
    flow, prompt = MODE_BUTTONS[MODE_COMMANDS[command.command]]
    await state.set_state(flow)
    await message.answer(prompt)


@router.message(F.text.in_(MODE_BUTTONS))
async def btn_mode(message: Message, state: FSMContext, **_: Any) -> None:
    flow, prompt = MODE_BUTTONS[message.text]
    await state.set_state(flow)
    await message.answer(prompt)


@router.message(Command("result"))
@router.message(F.text == kb.BTN_RESULT)
async def cmd_result(message: Message, state: FSMContext, bot: Bot, command: CommandObject | None = None, **data: Any) -> None:
    ctx = ctx_for(message, bot, **data)
    hook_id, metrics = parse_result_command(command.args if command else "")
    if hook_id is not None and metrics:
        await state.clear()
        await op_result(ctx, hook_id, metrics)
        return
    if hook_id is not None:
        await state.set_state(Flow.result)
        await state.update_data(hook_id=hook_id)
        await message.answer(texts.ASK_METRICS.format(hook_id=hook_id))
        return
    memory = ctx.storage.memory
    pending = memory.hooks_without_results(ctx.workspace, sources=learning.SCORED_SOURCES, limit=6)
    pending = pending or memory.hooks_without_results(ctx.workspace, limit=6)
    if not pending:
        await message.answer("Пока нет хуков без результатов. Оцените хук — и после публикации пришлите его статистику.")
        return
    await message.answer(texts.ASK_WHICH_HOOK, reply_markup=kb.pending_hooks(pending))


@router.message(Command("stats"))
@router.message(F.text == kb.BTN_PLAYBOOK)
async def cmd_stats(message: Message, engine: HookEngine, **_: Any) -> None:
    ctx = engine.context(workspace_of(message.from_user.id))
    text = render.render_stats(ctx)
    if ctx.playbook:
        text += "\n\n" + render.render_playbook(ctx.playbook)
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await message.answer(chunk, reply_markup=kb.learn_now() if last and ctx.n_results >= 3 else None)


@router.message(Command("playbook"))
async def cmd_playbook(message: Message, botdb: BotStorage, **_: Any) -> None:
    insight = botdb.memory.latest_insight(workspace_of(message.from_user.id))
    await message.answer(
        render.render_playbook(insight["content"] if insight else None), reply_markup=kb.learn_now()
    )


@router.message(Command("learn"))
async def cmd_learn(message: Message, bot: Bot, **data: Any) -> None:
    await op_learn(ctx_for(message, bot, **data))


def niche_status(memory: Any, niche_text: str) -> str:
    """Which niche of the knowledge base a free-text niche maps to, and how much the base knows about it."""
    niche = niches.resolve(niche_text)
    if not niche:
        return "В базе ниш такой пока нет: оценки опираются на общие данные и науку."
    model = breakout.latest(memory, niche)
    if not model:
        return f"Ниша базы: <b>{html.escape(niches.name(niche))}</b> — модели залёта пока нет, данные собираются."
    return (f"Ниша базы: <b>{html.escape(niches.name(niche))}</b> — модель на {model['n']} роликах "
            f"{model['n_creators']} авторов, качество прогноза: {model['quality']}.")


@router.message(Command("niche"))
async def cmd_niche(message: Message, command: CommandObject, state: FSMContext, botdb: BotStorage,
                    **data: Any) -> None:
    arg = (command.args or "").strip()
    ws = workspace_of(message.from_user.id)
    if arg.startswith("@"):
        scout = data.get("scout")
        if scout is None or scout.ig is None:
            await message.answer("Определить нишу по аккаунту можно, когда к боту подключён Instagram. "
                                 "Пока напишите нишу словами: /niche крипта и трейдинг")
            return
        try:
            result = await asyncio.to_thread(niches.detect, scout.ig, arg)
        except instagram.InstagramError as exc:
            await message.answer(f"😕 Не удалось прочитать аккаунт: {html.escape(str(exc))}")
            return
        if result["best"]:
            botdb.memory.update_profile(ws, niche=niches.name(result["best"]))
        await message.answer(render.render_niche_detect(result, breakout.available(botdb.memory)))
        return
    if arg:
        botdb.memory.update_profile(ws, niche=arg[:200])
        await message.answer(f"✅ Ниша сохранена: {html.escape(arg[:200])}\n{niche_status(botdb.memory, arg)}")
        return
    await state.set_state(Flow.niche)
    await message.answer(texts.ASK_NICHE)


@router.message(Command("share"))
async def cmd_share(message: Message, command: CommandObject, botdb: BotStorage, **_: Any) -> None:
    ws = workspace_of(message.from_user.id)
    arg = (command.args or "").strip().lower()
    if arg in ("on", "off", "вкл", "выкл"):
        botdb.memory.update_profile(ws, share_data=arg in ("on", "вкл"))
    enabled = botdb.memory.get_profile(ws)["share_data"]
    await message.answer(
        f"Обмен анонимными данными: <b>{'включён' if enabled else 'выключен'}</b>.\n"
        "Когда включён, ваши хуки и метрики помогают общей модели, которая работает у новых пользователей, "
        "пока у них нет своих данных. Изменить: <code>/share on</code> или <code>/share off</code>."
    )


@router.message(Command("deletedata"))
async def cmd_delete(message: Message, **_: Any) -> None:
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить всё", callback_data="del:yes")
    await message.answer(
        "Удалить все ваши хуки, результаты и плейбук? Баланс анализов сохранится.", reply_markup=builder.as_markup()
    )


@router.callback_query(F.data == "del:yes")
async def cb_delete(callback: CallbackQuery, botdb: BotStorage, **_: Any) -> None:
    botdb.memory.delete_workspace(workspace_of(callback.from_user.id))
    await callback.message.edit_text("Готово: данные удалены.")
    await callback.answer()


# --------------------------------------------------------------------------- #
# Your channel first: audit of the creator's own audience
# --------------------------------------------------------------------------- #


@router.message(Command("audit"))
async def cmd_audit(message: Message, bot: Bot, **data: Any) -> None:
    ctx = ctx_for(message, bot, **data)
    scout = ctx.scout
    if not (ctx.unlimited and scout is not None and scout.ig is not None):
        await message.answer(texts.AUDIT_HOWTO.format(n=audience.MIN_REELS))
        return
    await message.answer("🔎 Запускаю аудит вашего канала: забираю ролики и статистику из Instagram, "
                         "разбираю хук каждого ролика. Это займёт несколько минут — пришлю отчёт.")

    async def progress(text: str) -> None:
        await ctx.send(text)

    async def run() -> None:
        try:
            report = await scout.audit(ctx.workspace, progress=progress)
        except (instagram.InstagramError, EngineError) as exc:
            await ctx.send(f"😕 Аудит не удался: {html.escape(str(exc))}")
            return
        await ctx.send(render.render_audience(report))

    asyncio.create_task(run())


@router.message(Command("target"))
async def cmd_target(message: Message, bot: Bot, **data: Any) -> None:
    ctx = ctx_for(message, bot, **data)
    scout = ctx.scout
    if not (ctx.unlimited and scout is not None):
        await message.answer(texts.TARGET_HOWTO)
        return
    await message.answer("🎯 Определяю целевую аудиторию канала, ищу авторов с той же аудиторией и разбираю их ролики. "
                         "Это займёт несколько минут.")

    async def progress(text: str) -> None:
        await ctx.send(text)

    async def run() -> None:
        try:
            result = await scout.target_audience(ctx.workspace, own_account=scout.ig is not None, progress=progress)
        except (instagram.InstagramError, EngineError) as exc:
            await ctx.send(f"😕 Не получилось: {html.escape(str(exc))}")
            return
        await ctx.send(render.render_audience_profile(target.latest_profile(ctx.storage.memory, ctx.workspace)))
        await ctx.send(render.render_gap(result["gap"]))

    asyncio.create_task(run())


@router.message(Command("gap"))
async def cmd_gap(message: Message, botdb: BotStorage, **_: Any) -> None:
    ws = workspace_of(message.from_user.id)
    for text in (render.render_audience_profile(target.latest_profile(botdb.memory, ws)),
                 render.render_gap(target.latest_gap(botdb.memory, ws))):
        for chunk in split_message(text):
            await message.answer(chunk, disable_web_page_preview=True)


@router.message(Command("myreel"))
async def cmd_myreel(message: Message, state: FSMContext, **_: Any) -> None:
    await state.set_state(Flow.myreel)
    await message.answer(texts.ASK_MYREEL)


@router.message(Command("audience"))
async def cmd_audience(message: Message, botdb: BotStorage, **_: Any) -> None:
    report = audience.latest_report(botdb.memory, workspace_of(message.from_user.id))
    for chunk in split_message(render.render_audience(report)):
        await message.answer(chunk)


# --------------------------------------------------------------------------- #
# Science: the viral corpus, validation, fresh research
# --------------------------------------------------------------------------- #


@router.message(Command("ref"))
async def cmd_ref(message: Message, state: FSMContext, **_: Any) -> None:
    await state.set_state(Flow.ref)
    await message.answer(texts.ASK_REF)


@router.message(Command("science", "trends"))
async def cmd_science(message: Message, botdb: BotStorage, **_: Any) -> None:
    memory = botdb.memory
    niche = corpus.match_niche(memory, memory.get_profile(workspace_of(message.from_user.id))["niche"])
    for chunk in split_message(render.render_science(corpus.latest_report(memory, niche))):
        await message.answer(chunk)


@router.message(Command("validate"))
async def cmd_validate(message: Message, botdb: BotStorage, **_: Any) -> None:
    await message.answer(render.render_validation(validation.report(botdb.memory, workspace_of(message.from_user.id))))


@router.message(Command("watch", "unwatch"))
async def cmd_watch(message: Message, command: CommandObject, botdb: BotStorage, **_: Any) -> None:
    ws = workspace_of(message.from_user.id)
    c = corpus.Corpus(botdb.memory)
    username = (command.args or "").strip().lstrip("@")
    if username:
        if command.command == "watch":
            c.watch(ws, username, niche=botdb.memory.get_profile(ws)["niche"])
        else:
            c.unwatch(ws, username)
    watched = [f"@{w['username']}" for w in c.watchlist(ws)]
    await message.answer(
        ("Слежу за: " + ", ".join(watched) if watched else "Пока ни за кем не слежу.")
        + "\nДобавить аккаунт (бизнес или автор): <code>/watch username</code>. "
        "Робот будет находить у них залетевшие ролики и сравнивать с их обычными."
    )


@router.message(Command("papers", "approve"))
async def cmd_papers(message: Message, command: CommandObject, botdb: BotStorage, settings: Settings, **_: Any) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    if command.command == "approve" and command.args:
        chosen = literature.approve(botdb.memory, [int(x) for x in command.args.replace(",", " ").split() if x.isdigit()])
        await message.answer(f"Одобрено исследований: {len(chosen)}. Они попадут в промпты оценщика.")
    for chunk in split_message(render.render_papers(literature.pending(botdb.memory))):
        await message.answer(chunk, disable_web_page_preview=True)


@router.message(Command("scout"))
async def cmd_scout(message: Message, bot: Bot, **data: Any) -> None:
    ctx = ctx_for(message, bot, **data)
    if not ctx.unlimited or ctx.scout is None:
        return
    await message.answer("🛰 Запускаю цикл разведки…")

    async def run() -> None:
        summary = await ctx.scout.run_once()
        text = (f"Собрано: {summary.collected}, размечено: {summary.labeled}, изучено: {summary.studied}, "
                f"ошибок: {summary.failed}, своих результатов: {summary.own_results}, отчёты: {', '.join(summary.reports) or '—'}")
        if summary.errors:
            text += "\n" + "\n".join(html.escape(e) for e in summary.errors[:5])
        await ctx.send(text)

    asyncio.create_task(run())


@router.message(Command("balance", "buy"))
@router.message(F.text == kb.BTN_BALANCE)
async def cmd_balance(message: Message, botdb: BotStorage, settings: Settings, **_: Any) -> None:
    await message.answer(
        texts.BALANCE.format(credits=botdb.credits(message.from_user.id)), reply_markup=kb.packs(settings.packs)
    )


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message, settings: Settings, **_: Any) -> None:
    await message.answer(texts.PAYSUPPORT.format(contact=html.escape(settings.support_contact or "в поддержку бота")))


@router.message(Command("admin"))
async def cmd_admin(message: Message, botdb: BotStorage, settings: Settings, **_: Any) -> None:
    if message.from_user.id not in settings.admin_ids:
        return
    stats = botdb.admin_stats()
    await message.answer("\n".join(f"{k}: <b>{v}</b>" for k, v in stats.items()))


@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot, botdb: BotStorage, settings: Settings, **_: Any) -> None:
    if message.from_user.id not in settings.admin_ids or not command.args:
        return
    charge_id = command.args.strip()
    payment = botdb.get_payment(charge_id)
    if payment is None or payment["refunded"]:
        await message.answer("Платёж не найден или уже возвращён.")
        return
    await bot.refund_star_payment(payment["user_id"], charge_id)
    botdb.mark_refunded(charge_id)
    await message.answer(f"Возвращено {payment['stars']} ⭐ пользователю {payment['user_id']}.")


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("mode:"))
async def cb_mode(callback: CallbackQuery, state: FSMContext, bot: Bot, **data: Any) -> None:
    await callback.answer()
    text = (await state.get_data()).get("pending_text")
    await state.clear()
    if not text:
        await callback.message.answer("Пришлите текст ещё раз.")
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await OPS[callback.data.split(":", 1)[1]](ctx_for(callback, bot, **data), text)


async def _hook_from_callback(callback: CallbackQuery, botdb: BotStorage) -> tuple[int, HookInput] | None:
    hook_id = int(callback.data.split(":", 1)[1])
    hook = botdb.memory.get_hook(hook_id, workspace_of(callback.from_user.id))
    if hook is None:
        await callback.answer("Хук не найден", show_alert=True)
        return None
    await callback.answer()
    return hook_id, HookInput(
        text=hook["text"], on_screen_text=hook["on_screen_text"], visual=hook["visual"], niche=hook["niche"]
    )


@router.callback_query(F.data.startswith("rw:"))
async def cb_rewrite(callback: CallbackQuery, bot: Bot, **data: Any) -> None:
    found = await _hook_from_callback(callback, data["botdb"])
    if found:
        await op_rewrite(ctx_for(callback, bot, **data), found[1], parent_id=found[0])


@router.callback_query(F.data.startswith("act:"))
async def cb_actions(callback: CallbackQuery, bot: Bot, **data: Any) -> None:
    found = await _hook_from_callback(callback, data["botdb"])
    if found:
        await op_actions(ctx_for(callback, bot, **data), found[1])


@router.callback_query(F.data.startswith("res:"))
async def cb_result(callback: CallbackQuery, state: FSMContext, **_: Any) -> None:
    hook_id = int(callback.data.split(":", 1)[1])
    await state.set_state(Flow.result)
    await state.update_data(hook_id=hook_id)
    await callback.answer()
    await callback.message.answer(texts.ASK_METRICS.format(hook_id=hook_id))


@router.callback_query(F.data.startswith("skipfu:"))
async def cb_skip_followup(callback: CallbackQuery, botdb: BotStorage, **_: Any) -> None:
    botdb.close_followup(int(callback.data.split(":", 1)[1]))
    await callback.answer("Ок, не буду напоминать")
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "learn")
async def cb_learn(callback: CallbackQuery, bot: Bot, **data: Any) -> None:
    await callback.answer()
    await op_learn(ctx_for(callback, bot, **data))


# --------------------------------------------------------------------------- #
# Payments: Telegram Stars (XTR), required for digital goods in bots
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery, bot: Bot, settings: Settings, **_: Any) -> None:
    pack = settings.pack(callback.data.split(":", 1)[1])
    await callback.answer()
    if pack is None:
        return
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=f"{pack.credits} анализов хуков",
        description="Оценка, генерация и переписывание хуков, подбор звука/действий/текста. Не сгорают.",
        payload=pack.payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"{pack.credits} анализов", amount=pack.stars)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, settings: Settings, **_: Any) -> None:
    pack = settings.pack_from_payload(query.invoice_payload)
    if pack is None or query.currency != "XTR" or query.total_amount != pack.stars:
        await query.answer(ok=False, error_message="Этот пакет больше недоступен. Откройте /balance заново.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, botdb: BotStorage, settings: Settings, **_: Any) -> None:
    payment = message.successful_payment
    # Credit what the invoice promised, even if the pack was reconfigured after it was issued.
    pack = Pack.from_payload(payment.invoice_payload)
    credits = pack.credits if pack else 0
    pack_id = pack.id if pack else payment.invoice_payload
    if pack is None:
        log.error("Unknown payment payload %r (charge %s)", payment.invoice_payload, payment.telegram_payment_charge_id)
    if botdb.record_payment(
        message.from_user.id, pack_id, payment.total_amount, credits, payment.telegram_payment_charge_id
    ):
        log.info("Payment %s: user %s, %s stars", payment.telegram_payment_charge_id, message.from_user.id, payment.total_amount)
    await message.answer(
        f"🎉 Спасибо! Начислено {credits} анализов. Баланс: <b>{botdb.credits(message.from_user.id)}</b>.",
        reply_markup=kb.main_menu(),
    )


# --------------------------------------------------------------------------- #
# Free-form input
# --------------------------------------------------------------------------- #


@router.message(F.video | F.video_note | (F.document.mime_type.startswith("video/")))
async def on_video(message: Message, state: FSMContext, bot: Bot, **data: Any) -> None:
    current = await state.get_state()
    await state.clear()
    ctx = ctx_for(message, bot, **data)
    if current == Flow.ref.state:
        await op_ref(ctx, message)
    elif current == Flow.myreel.state:
        await op_myreel(ctx, message)
    else:
        await op_video(ctx, message)


@router.message(Flow.result, F.text)
async def on_metrics(message: Message, state: FSMContext, bot: Bot, **data: Any) -> None:
    hook_id = (await state.get_data()).get("hook_id")
    metrics = parse_metrics(message.text)
    if not metrics:
        number = parse_number(message.text)
        if number is not None and number <= 100:
            metrics = {"skip_rate": number}
    if not metrics:
        await message.answer("Не нашёл цифр. Пример: <code>skip 38% просмотры 12к</code>. Отмена — /cancel")
        return
    await state.clear()
    await op_result(ctx_for(message, bot, **data), hook_id, metrics)


@router.message(Flow.niche, F.text)
async def on_niche(message: Message, state: FSMContext, botdb: BotStorage, **_: Any) -> None:
    await state.clear()
    niche = message.text.strip()[:200]
    botdb.memory.update_profile(workspace_of(message.from_user.id), niche=niche)
    await message.answer(f"✅ Ниша сохранена: {html.escape(niche)}\n{niche_status(botdb.memory, niche)}",
                         reply_markup=kb.main_menu())


FLOW_OPS = {Flow.score.state: "score", Flow.generate.state: "generate", Flow.rewrite.state: "rewrite", Flow.actions.state: "actions"}


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, state: FSMContext, bot: Bot, **data: Any) -> None:
    text = message.text[:MAX_INPUT_CHARS]
    op = FLOW_OPS.get(await state.get_state())
    if op:
        await state.clear()
        await OPS[op](ctx_for(message, bot, **data), text)
        return
    # No mode chosen: a short text is most likely a hook, a long one a script — let the user confirm.
    await state.update_data(pending_text=text)
    guess = "сценарий" if len(text) > 300 or text.count("\n") >= 4 else "хук"
    await message.answer(f"Похоже на {guess}. Что сделать?", reply_markup=kb.choose_mode())
