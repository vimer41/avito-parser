import os
import math
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
TESTING = os.getenv("AVITO_RADAR_TESTING") == "1"
if not TESTING:
    load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "25"))
MAX_SEARCHES_PER_USER = int(os.getenv("MAX_SEARCHES_PER_USER", "20"))
# Это воркеры обработки, а не разрешённая Авито частота запросов.
# PROXY_POOL_FILE не создаёт пул: активный HTTP-путь использует только PROXY_URL.
MAX_CONCURRENT_WORKERS = min(4, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "4"))))


def positive_float_setting(name: str, default: str) -> float:
    value = float(os.getenv(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


if CHECK_INTERVAL <= 0:
    raise ValueError("CHECK_INTERVAL must be positive")

# Осторожный стартовый бюджет, НЕ подтверждённый безопасный лимит Авито.
# Увеличивать только после разрешённых измерений. Бюджет общий для всех сессий.
AVITO_REQUESTS_PER_SECOND = positive_float_setting("AVITO_REQUESTS_PER_SECOND", "0.5")
AVITO_MAX_IN_FLIGHT = min(4, max(1, int(os.getenv("AVITO_MAX_IN_FLIGHT", "2"))))
AVITO_BLOCK_BASE_COOLDOWN = positive_float_setting("AVITO_BLOCK_BASE_COOLDOWN", "30")
AVITO_BLOCK_MAX_COOLDOWN = max(AVITO_BLOCK_BASE_COOLDOWN, positive_float_setting("AVITO_BLOCK_MAX_COOLDOWN", "900"))
AVITO_FETCH_TIMEOUT = positive_float_setting("AVITO_FETCH_TIMEOUT", "45")
AVITO_MAX_REQUESTS_PER_FETCH = max(1, int(os.getenv("AVITO_MAX_REQUESTS_PER_FETCH", "8")))
AVITO_MAX_PAGES_PER_SCAN = min(5, max(1, int(os.getenv("AVITO_MAX_PAGES_PER_SCAN", "2"))))
SCAN_MAX_INTERVAL = max(float(CHECK_INTERVAL), positive_float_setting("SCAN_MAX_INTERVAL", "120"))
SCAN_BUDGET_UTILIZATION = positive_float_setting("SCAN_BUDGET_UTILIZATION", "0.8")
if SCAN_BUDGET_UTILIZATION > 1:
    raise ValueError("SCAN_BUDGET_UTILIZATION must not exceed 1")
SCAN_MAX_RETRIES = min(3, max(0, int(os.getenv("SCAN_MAX_RETRIES", "2"))))
SCAN_RETRY_BASE_DELAY = positive_float_setting("SCAN_RETRY_BASE_DELAY", "5")
SCAN_RETRY_MAX_DELAY = max(SCAN_RETRY_BASE_DELAY, positive_float_setting("SCAN_RETRY_MAX_DELAY", "60"))
SCAN_STARTUP_SPREAD = positive_float_setting("SCAN_STARTUP_SPREAD", "15")
INITIAL_SNAPSHOT_TTL = positive_float_setting("INITIAL_SNAPSHOT_TTL", "25")

# Stars — единственный способ оплаты цифровой подписки. Включается явно.
STARS_PAYMENTS_ENABLED = os.getenv("STARS_PAYMENTS_ENABLED", "0").strip() == "1"
STARS_PRICE_STANDART = int(os.getenv("STARS_PRICE_STANDART", "170"))
STARS_PRICE_PRO = int(os.getenv("STARS_PRICE_PRO", "470"))
STARS_PRICE_TEST = int(os.getenv("STARS_PRICE_TEST", "10"))
STARS_SUBSCRIPTION_DAYS = 30
if STARS_PRICE_STANDART <= 0 or STARS_PRICE_PRO <= 0 or STARS_PRICE_TEST <= 0:
    raise ValueError("Telegram Stars prices must be positive integers")
if STARS_PRICE_PRO <= STARS_PRICE_STANDART:
    raise ValueError("PRO must cost more Stars than Standart")


def stars_price(plan: str) -> int:
    if plan == "test":
        return STARS_PRICE_TEST
    if plan == "standart":
        return STARS_PRICE_STANDART
    if plan == "pro":
        return STARS_PRICE_PRO
    raise ValueError("Unknown tariff plan")


def parse_proxy(raw_proxy: str | None) -> str | None:
    """
    Нормализует прокси в стандартный RFC-формат (http://LOGIN:PASS@IP:PORT).
    Поддерживает:
    - http://IP:PORT:LOGIN:PASS
    - IP:PORT:LOGIN:PASS
    - http://LOGIN:PASS@IP:PORT
    - socks5://...
    """
    if not raw_proxy or not raw_proxy.strip():
        return None
    raw = raw_proxy.strip()

    scheme = "http"
    clean = raw
    if "://" in clean:
        scheme, clean = clean.split("://", 1)

    if "@" in clean:
        return f"{scheme}://{clean}"

    parts = clean.split(":")
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"{scheme}://{user}:{password}@{ip}:{port}"
    elif len(parts) == 2:
        ip, port = parts
        return f"{scheme}://{ip}:{port}"

    return raw

RAW_PROXY = os.getenv("PROXY_URL", "").strip() or None
PROXY_URL = parse_proxy(RAW_PROXY)

# Значение ADMIN_IDS имеет приоритет; ADMIN_ID поддерживается для совместимости.
raw_admin_ids = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or "986832959,455386428"
try:
    ADMIN_IDS = list(dict.fromkeys(int(aid.strip()) for aid in raw_admin_ids.split(",") if aid.strip()))
except ValueError as exc:
    raise ValueError("ADMIN_IDS / ADMIN_ID must contain comma-separated integer IDs") from exc
if not ADMIN_IDS or any(aid <= 0 for aid in ADMIN_IDS):
    raise ValueError("At least one positive administrator ID is required")
ADMIN_ID = ADMIN_IDS[0]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# Database path
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "bot.db"))).expanduser().resolve()
