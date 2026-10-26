"""
Модуль обхода анти-бот систем Avito (QRATOR, Firewall PoW, GeeTest v4).
"""

from importlib import import_module

__all__ = [
    "BASE_URL",
    "get_browser_user_agent",
    "is_qrator_challenge_response",
    "solve_qrator_challenge",
    "extract_pow_challenge",
    "solve_pow_challenge",
    "is_geetest_challenge",
    "solve_geetest_if_available",
    "handle_anti_bot_challenge",
]


def __getattr__(name):
    # The offline PoW reference must remain importable without HTTP/ML libraries.
    if name in __all__:
        value = getattr(import_module("anti_bot.manager"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
