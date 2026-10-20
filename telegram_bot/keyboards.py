from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from .config import Pack

BTN_SCORE = "🎯 Оценить хук"
BTN_GENERATE = "✨ Хуки по сценарию"
BTN_REWRITE = "♻️ Переписать хук"
BTN_ACTIONS = "🎬 Звук, действие, текст"
BTN_RESULT = "📈 Внести результат"
BTN_PLAYBOOK = "🧠 Чему научился бот"
BTN_BALANCE = "💎 Баланс"
BTN_HELP = "❓ Помощь"


def main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for text in (BTN_SCORE, BTN_GENERATE, BTN_REWRITE, BTN_ACTIONS, BTN_RESULT, BTN_PLAYBOOK, BTN_BALANCE, BTN_HELP):
        kb.button(text=text)
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, input_field_placeholder="Пришлите хук, сценарий или видео")


def choose_mode() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 Оценить как хук", callback_data="mode:score")
    kb.button(text="✨ Это сценарий — дай хуки", callback_data="mode:generate")
    kb.button(text="♻️ Переписать", callback_data="mode:rewrite")
    kb.button(text="🎬 Что добавить", callback_data="mode:actions")
    kb.adjust(1, 1, 2)
    return kb.as_markup()


def after_evaluation(hook_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="♻️ Переписать", callback_data=f"rw:{hook_id}")
    kb.button(text="🎬 Что добавить", callback_data=f"act:{hook_id}")
    kb.button(text="📈 Внести результат", callback_data=f"res:{hook_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def after_variants(best_hook_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎬 Что добавить к лучшему", callback_data=f"act:{best_hook_id}")
    kb.button(text="📈 Опубликовал — внести результат", callback_data=f"res:{best_hook_id}")
    kb.adjust(1)
    return kb.as_markup()


def followup(hook_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 Внести результат", callback_data=f"res:{hook_id}")
    kb.button(text="🙅 Не публиковал", callback_data=f"skipfu:{hook_id}")
    kb.adjust(2)
    return kb.as_markup()


def pending_hooks(hooks: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for hook in hooks:
        kb.button(text=f"#{hook['id']} {hook['text'][:40]}", callback_data=f"res:{hook['id']}")
    kb.adjust(1)
    return kb.as_markup()


def packs(items: tuple[Pack, ...]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pack in items:
        kb.button(text=pack.title, callback_data=f"buy:{pack.id}")
    kb.adjust(1)
    return kb.as_markup()


def learn_now() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить плейбук", callback_data="learn")
    return kb.as_markup()
