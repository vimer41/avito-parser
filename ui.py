"""Premium (animated) custom emoji for the bot UI.

Every icon in the interface is a named slot with a plain emoji fallback. An admin fills the slots
from the bot (🎨 in the admin panel): send any emoji of a premium pack and every slot whose plain
emoji exists in that pack gets the animated version. Until a slot is filled, the plain emoji is used,
so the bot looks the same as before and tests see plain text.

Bot API 9.4+: a bot may use custom emoji in messages and on buttons when its owner has Telegram
Premium. If Telegram ever rejects them, `PremiumEmojiFallback` resends the request with plain emoji
and switches premium emoji off until an admin turns them back on.
"""

import html
import json
import logging
import re
from typing import Iterable, Optional

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

SETTING_IDS = "ui_emoji"
SETTING_ENABLED = "ui_emoji_enabled"

# name -> (plain emoji, what it marks). The order is the order of the admin screen.
SLOTS: dict[str, tuple[str, str]] = {
    "logo": ("📡", "логотип, главный экран"),
    "hello": ("👋", "приветствие"),
    "radar": ("🎯", "радар, добавить радар"),
    "radars": ("📁", "мои радары"),
    "add": ("➕", "добавить"),
    "tariffs": ("💎", "тарифы"),
    "profile": ("👤", "профиль, продавец"),
    "help": ("📖", "инструкция"),
    "docs": ("📄", "документы"),
    "support": ("💬", "поддержка"),
    "home": ("🏠", "главное меню"),
    "trial": ("🎁", "пробный период"),
    "lite": ("⚡", "тариф Базовый"),
    "standart": ("🚀", "тариф Средний"),
    "pro": ("🔥", "тариф Полный"),
    "on": ("🟢", "радар работает"),
    "pause": ("⏸", "пауза"),
    "play": ("▶️", "запустить"),
    "shield": ("🛡", "стоп-слова"),
    "ban": ("🚫", "чёрный список"),
    "link": ("🔗", "открыть на Авито"),
    "trash": ("🗑", "удалить"),
    "refresh": ("🔄", "обновить"),
    "ok": ("✅", "готово"),
    "warn": ("⚠️", "предупреждение"),
    "cancel": ("❌", "отмена"),
    "wait": ("⏳", "ожидание"),
    "new": ("⚡", "новый лот"),
    "price": ("💰", "цена"),
    "place": ("📍", "адрес"),
    "time": ("🕒", "время"),
    "box": ("📦", "детали лота"),
    "views": ("👁", "просмотры"),
    "note": ("📝", "описание"),
    "card": ("💳", "оплата СБП"),
    "star": ("⭐", "Telegram Stars"),
    "coin": ("🪙", "криптовалюта"),
    "lock": ("🔒", "конфиденциальность, резерв"),
    "scope": ("🔍", "область поиска, поиск"),
    "write": ("✍️", "написать"),
    "mail": ("📧", "почта"),
    "admin": ("👑", "админ-панель"),
    "users": ("👥", "пользователи"),
    "net": ("🌐", "сеть и прокси"),
    "stats": ("📊", "мониторинг"),
    "db": ("💾", "база"),
    "logs": ("📋", "логи"),
    "palette": ("🎨", "премиум-эмодзи"),
}

# Reply-keyboard labels and their legacy spellings. A reply button sends its label back as text,
# so these keep plain emoji: handlers match on the exact string.
MENU: dict[str, tuple[str, set[str]]] = {
    "add": ("🎯 Добавить радар", {"➕ Добавить радар", "➕ Добавить поиск"}),
    "radars": ("📁 Мои радары", {"📂 Мои радары", "📋 Мои поиски", "Мои радары"}),
    "tariffs": ("💳 Тарифы", {"💳 Тарифы и подписка", "Тарифы", "Подписка", "💳 Подписка", "💎 Тарифы"}),
    "profile": ("👤 Мой профиль", {"📊 Мой статус", "Мой профиль"}),
    "help": ("📖 Инструкция", {"❓ Помощь", "Инструкция"}),
    "docs": ("📄 Документы", {"Документы"}),
    "support": ("💬 Поддержка", {"Поддержка", "Контакты", "Помощь", "SOS"}),
    "admin": ("👑 Админ-панель", {"👑 Админка"}),
}
CANCEL_TEXTS = {"❌ Отмена", "Отмена"}


def menu_texts(*keys: str) -> set[str]:
    out: set[str] = set()
    for key in keys:
        label, aliases = MENU[key]
        out |= {label, *aliases}
    return out


ALL_MENU_TEXTS = menu_texts(*MENU) | CANCEL_TEXTS

ENABLED = True
IDS: dict[str, str] = {}
_CHAR_BY_ID: dict[str, str] = {}
_TAG_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _norm(char: str) -> str:
    return (char or "").replace("️", "").strip()


def _apply(ids: dict, enabled: bool) -> None:
    global ENABLED, IDS, _CHAR_BY_ID
    ENABLED = bool(enabled)
    IDS = {k: str(v) for k, v in (ids or {}).items() if k in SLOTS and str(v).isdigit()}
    _CHAR_BY_ID = {v: SLOTS[k][0] for k, v in IDS.items()}


def plain(name: str) -> str:
    return SLOTS[name][0]


def custom_id(name: str) -> Optional[str]:
    return IDS.get(name) if ENABLED else None


def e(name: str) -> str:
    """Emoji for HTML text: animated when the slot is filled, plain otherwise."""
    char = SLOTS[name][0]
    cid = custom_id(name)
    return f'<tg-emoji emoji-id="{cid}">{char}</tg-emoji>' if cid else char


def btn(text: str, icon: Optional[str] = None, *, style: Optional[str] = None, **kwargs) -> InlineKeyboardButton:
    """Inline button with an icon slot: animated icon when filled, plain emoji in the text otherwise."""
    cid = custom_id(icon) if icon else None
    if icon and not cid:
        text = f"{SLOTS[icon][0]} {text}"
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=cid, style=style, **kwargs)


def kb(*rows: Iterable[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def home_btn() -> InlineKeyboardButton:
    return btn("Главное меню", "home", callback_data="start_approved")


async def show(event, text: str, markup=None, *, answer: bool = True, **kwargs) -> None:
    """Edit the screen in place for a button press, send a new message for a typed command."""
    if isinstance(event, CallbackQuery):
        if answer:
            try:
                await event.answer()
            except Exception:
                pass
        if event.message is not None:
            try:
                await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML", **kwargs)
                return
            except Exception as exc:
                if "not modified" in str(exc).lower():
                    return
        message = event.message
    else:
        message = event
    if message is not None:
        await message.answer(text, reply_markup=markup, parse_mode="HTML", **kwargs)


def visible_len(text: str) -> int:
    """Length Telegram counts for a caption: without HTML tags and entities."""
    return len(html.unescape(_ANY_TAG_RE.sub("", text or "")))


def strip_html(text: str) -> str:
    return _TAG_RE.sub(r"\1", text or "")


# ── Persistence ────────────────────────────────────────────────────────

async def load() -> None:
    import database
    try:
        ids = json.loads(await database.get_bot_setting(SETTING_IDS, "{}") or "{}")
    except (TypeError, ValueError):
        ids = {}
    enabled = (await database.get_bot_setting(SETTING_ENABLED, "1")) != "0"
    _apply(ids if isinstance(ids, dict) else {}, enabled)
    logger.info("Премиум-эмодзи: заполнено %s из %s слотов, включены=%s", len(IDS), len(SLOTS), ENABLED)


async def save(ids: Optional[dict] = None, enabled: Optional[bool] = None) -> None:
    import database
    if ids is not None:
        await database.set_bot_setting(SETTING_IDS, json.dumps(ids))
    if enabled is not None:
        await database.set_bot_setting(SETTING_ENABLED, "1" if enabled else "0")
    _apply(IDS if ids is None else ids, ENABLED if enabled is None else enabled)


def match_slots(stickers: Iterable[tuple[str, str]], only_empty: bool = False) -> dict[str, str]:
    """(plain emoji, custom_emoji_id) pairs -> {slot: id} for slots whose plain emoji matches."""
    by_char: dict[str, str] = {}
    for char, cid in stickers:
        by_char.setdefault(_norm(char), str(cid))
    found = {}
    for name, (char, _desc) in SLOTS.items():
        if only_empty and name in IDS:
            continue
        cid = by_char.get(_norm(char))
        if cid:
            found[name] = cid
    return found


def custom_emoji_in(message) -> list[tuple[str, str]]:
    """(plain emoji, custom_emoji_id) for every custom emoji in a message."""
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    utf16 = text.encode("utf-16-le")
    out = []
    for ent in entities:
        if ent.type == "custom_emoji" and ent.custom_emoji_id:
            char = utf16[ent.offset * 2:(ent.offset + ent.length) * 2].decode("utf-16-le", "ignore")
            out.append((char, ent.custom_emoji_id))
    return out


# ── Fallback when Telegram rejects custom emoji ────────────────────────

def _strip_markup(markup):
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup, False
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            if b.icon_custom_emoji_id:
                char = _CHAR_BY_ID.get(b.icon_custom_emoji_id, "")
                b = b.model_copy(update={"icon_custom_emoji_id": None, "text": f"{char} {b.text}".strip()})
                changed = True
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows), changed


def strip_method(method):
    """A copy of a Bot API call without custom emoji, or None when it has none."""
    update = {}
    for field in ("text", "caption"):
        value = getattr(method, field, None)
        if isinstance(value, str) and "<tg-emoji" in value:
            update[field] = strip_html(value)
    markup, changed = _strip_markup(getattr(method, "reply_markup", None))
    if changed:
        update["reply_markup"] = markup
    return method.model_copy(update=update) if update else None


class PremiumEmojiFallback(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                raise
            plain_method = strip_method(method)
            if plain_method is None:
                raise
            result = await make_request(bot, plain_method)
            global ENABLED
            if ENABLED:
                ENABLED = False
                logger.error("Telegram отклонил премиум-эмодзи (%s): выключаю их до ручного включения в админке", exc)
            return result
