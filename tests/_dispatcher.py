"""Real dispatcher / immutable Telegram models with an offline Bot API session."""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from tests import _environment
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import config
from middlewares import AccessControlMiddleware


class OfflineTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.failures = {}

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name in self.failures:
            raise self.failures[name]
        if name == "GetMe":
            return User(id=bot.id, is_bot=True, first_name="Fixture Bot", username="fixture_bot")
        if name.startswith(("Send", "EditMessage")):
            chat_id = getattr(method, "chat_id", 123456)
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(chat_id), type="private"),
                           from_user=User(id=bot.id, is_bot=True, first_name="Fixture Bot"),
                           text=getattr(method, "text", None))
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        raise AssertionError("Downloads are forbidden in dispatcher tests")
        yield b""


def load_router(name):
    """Router objects may have one parent only; independently import each fixture."""
    import handlers
    module_name = f"handlers._fixture_{name}_{id(object())}_{len(sys.modules)}"
    spec = importlib.util.spec_from_file_location(module_name, Path(handlers.__file__).parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    del sys.modules[module_name]
    return module.router


class DispatcherFixture:
    def setup_dispatcher(self, names=("payments", "admin", "support", "common", "searches")):
        self.session = OfflineTelegramSession()
        self.bot = Bot(config.BOT_TOKEN, session=self.session)
        self.dp = Dispatcher(storage=MemoryStorage())
        access = AccessControlMiddleware()
        self.dp.message.middleware(access)
        self.dp.callback_query.middleware(access)
        main = Router()
        for name in names:
            main.include_router(load_router(name))
        self.dp.include_router(main)
        self.update_id = 0

    async def close_dispatcher(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    async def dispatch_callback(self, data, user_id=123456, chat_id=None):
        self.update_id += 1
        user = User(id=user_id, is_bot=False, first_name="Fixture")
        message = Message(message_id=1, date=datetime.now(timezone.utc),
                          chat=Chat(id=chat_id or user_id, type="private"),
                          from_user=User(id=self.bot.id, is_bot=True, first_name="Fixture Bot"), text="Fixture")
        call = CallbackQuery(id=f"callback-{self.update_id}", from_user=user,
                             chat_instance="fixture", message=message, data=data)
        update = Update(update_id=self.update_id, callback_query=call)
        await self.dp.feed_update(self.bot, update)
        return call

    async def dispatch_message(self, *, user_id=123456, text=None, successful_payment=None, refunded_payment=None):
        self.update_id += 1
        message = Message(message_id=self.update_id, date=datetime.now(timezone.utc),
                          chat=Chat(id=user_id, type="private"),
                          from_user=User(id=user_id, is_bot=False, first_name="Fixture"),
                          text=text, successful_payment=successful_payment, refunded_payment=refunded_payment)
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message))

    def methods(self, name):
        return [method for method in self.session.calls if type(method).__name__ == name]

    def last_markup(self):
        methods = [method for method in self.session.calls if getattr(method, "reply_markup", None)]
        return methods[-1].reply_markup

    def callback_values(self):
        return [button.callback_data for row in self.last_markup().inline_keyboard for button in row]