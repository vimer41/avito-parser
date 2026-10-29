import math
import os
import warnings
from pathlib import Path
from typing import Mapping
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
TESTING = os.getenv("AVITO_RADAR_TESTING") == "1"
if not TESTING:
    load_dotenv(BASE_DIR / ".env")

DEPRECATED_ENV_VARS = (
    "CHECK_INTERVAL",
    "AVITO_REQUESTS_PER_SECOND",
    "MAX_CONCURRENT_WORKERS",
    "AVITO_MAX_IN_FLIGHT",
    "SCAN_MAX_INTERVAL",
    "PARSE_WORKERS",
)


def check_deprecated_env_vars(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    messages: list[str] = []
    for name in DEPRECATED_ENV_VARS:
        if name in env and str(env.get(name, "")).strip() != "":
            msg = (
                f"⚠️ Устаревшая переменная окружения {name}={env[name]!r} игнорируется; "
                "используйте параметры пула прокси и планировщика (SCAN_INTERVAL_*, PROXY_*)."
            )
            messages.append(msg)
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
    return messages


def positive_float_setting(name: str, default: str, environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть числом") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"Параметр {name} должен быть положительным конечным числом")
    return value


def non_negative_float_setting(name: str, default: str, environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    try:
        value = float(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть числом") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Параметр {name} должен быть неотрицательным конечным числом")
    return value


def positive_int_setting(name: str, default: str, environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть целым числом") from exc
    if value <= 0:
        raise ValueError(f"Параметр {name} должен быть положительным целым числом (> 0)")
    return value


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_SEARCHES_PER_USER = positive_int_setting("MAX_SEARCHES_PER_USER", "20")

# --- Планировщик и интервалы ---
SCAN_INTERVAL_MIN = positive_float_setting("SCAN_INTERVAL_MIN", "5.0")
SCAN_INTERVAL_MAX = positive_float_setting("SCAN_INTERVAL_MAX", "10.0")
SCAN_CAPACITY_MODE_WINDOWS = positive_int_setting("SCAN_CAPACITY_MODE_WINDOWS", "2")
PROXY_INITIAL_HTTP_COST_PER_SCAN = positive_float_setting("PROXY_INITIAL_HTTP_COST_PER_SCAN", "1.3")
HTTP_COST_MIN_SAMPLES = positive_int_setting("HTTP_COST_MIN_SAMPLES", "30")
SOURCE_BACKOFF_BASE = positive_float_setting("SOURCE_BACKOFF_BASE", "30.0")
SOURCE_BACKOFF_MAX = positive_float_setting("SOURCE_BACKOFF_MAX", "300.0")
SOURCE_RECOVERY_SUCCESSES = positive_int_setting("SOURCE_RECOVERY_SUCCESSES", "3")
SCAN_PROXY_ATTEMPTS = positive_int_setting("SCAN_PROXY_ATTEMPTS", "2")

# --- Прокси и HTTP ---
PROXY_POOL_FILE = (os.getenv("PROXY_POOL_FILE") or "").strip()
ALLOW_DIRECT_AVITO = os.getenv("ALLOW_DIRECT_AVITO", "1" if TESTING else "0").strip() == "1"
PROXY_RATE_LIMIT = positive_float_setting("PROXY_RATE_LIMIT", "1.5")
PROXY_RATE_RECOMMENDATION_MAX = positive_float_setting("PROXY_RATE_RECOMMENDATION_MAX", "2.0")
PROXY_CAPACITY_RESERVE = non_negative_float_setting("PROXY_CAPACITY_RESERVE", "0.20")
PROXY_MIN_RATE = positive_float_setting("PROXY_MIN_RATE", "0.1")
PROXY_MAX_IN_FLIGHT = positive_int_setting("PROXY_MAX_IN_FLIGHT", "1")
PROXY_FAILURE_THRESHOLD = positive_int_setting("PROXY_FAILURE_THRESHOLD", "3")
PROXY_BLOCK_WINDOW_SECONDS = positive_int_setting("PROXY_BLOCK_WINDOW_SECONDS", "600")
PROXY_BLOCK_RATIO_THRESHOLD = positive_float_setting("PROXY_BLOCK_RATIO_THRESHOLD", "0.05")
PROXY_BLOCK_RATIO_MIN_REQUESTS = positive_int_setting("PROXY_BLOCK_RATIO_MIN_REQUESTS", "20")
PROXY_RECOVERY_SUCCESS_STREAK = positive_int_setting("PROXY_RECOVERY_SUCCESS_STREAK", "50")
PROXY_RECOVERY_FACTOR = positive_float_setting("PROXY_RECOVERY_FACTOR", "1.25")
PROXY_QUARANTINE_BASE = positive_float_setting("PROXY_QUARANTINE_BASE", "2")
PROXY_QUARANTINE_MAX = positive_float_setting("PROXY_QUARANTINE_MAX", "15")
PROXY_TRANSPORT_BACKOFF_BASE = positive_float_setting("PROXY_TRANSPORT_BACKOFF_BASE", "2")
PROXY_TRANSPORT_BACKOFF_MAX = positive_float_setting("PROXY_TRANSPORT_BACKOFF_MAX", "10")
# Token bucket per proxy client: sustained PROXY_RATE_LIMIT req/s with bursts up to PROXY_RATE_BURST requests.
PROXY_RATE_BURST = positive_float_setting("PROXY_RATE_BURST", "3")
CHALLENGE_WORKERS = positive_int_setting("CHALLENGE_WORKERS", "1")
COOKIE_SAVE_INTERVAL = non_negative_float_setting("COOKIE_SAVE_INTERVAL", "30")

AVITO_CONNECT_TIMEOUT = positive_float_setting("AVITO_CONNECT_TIMEOUT", "3.0")
AVITO_READ_TIMEOUT = positive_float_setting("AVITO_READ_TIMEOUT", "8.0")
AVITO_COLLECTION_DEADLINE = positive_float_setting("AVITO_COLLECTION_DEADLINE", "20.0")
AVITO_MAX_REQUESTS_PER_SCAN = positive_int_setting("AVITO_MAX_REQUESTS_PER_SCAN", "8")
AVITO_MAX_PAGES_PER_SCAN = min(5, positive_int_setting("AVITO_MAX_PAGES_PER_SCAN", "2"))

# --- Доставка и outbox ---
TARIFF_DELAY_PRO = non_negative_float_setting("TARIFF_DELAY_PRO", "0.0")
TARIFF_DELAY_STANDART = non_negative_float_setting("TARIFF_DELAY_STANDART", "1.0")
TARIFF_DELAY_BASE = non_negative_float_setting("TARIFF_DELAY_BASE", "1.5")
TARIFF_DELIVERY_DELAY: dict[str, float] = {
    "pro": TARIFF_DELAY_PRO,
    "standart": TARIFF_DELAY_STANDART,
    "base": TARIFF_DELAY_BASE,
}

NOTIFICATION_WORKERS = positive_int_setting("NOTIFICATION_WORKERS", "10")
TELEGRAM_GLOBAL_RATE = positive_float_setting("TELEGRAM_GLOBAL_RATE", "25.0")
TELEGRAM_CHAT_INTERVAL = positive_float_setting("TELEGRAM_CHAT_INTERVAL", "1.0")
NOTIFICATION_MAX_AGE = positive_int_setting("NOTIFICATION_MAX_AGE", "1800")
NOTIFICATION_LEASE_SECONDS = positive_int_setting("NOTIFICATION_LEASE_SECONDS", "60")
NOTIFICATION_RETRY_MAX = positive_int_setting("NOTIFICATION_RETRY_MAX", "60")
NOTIFICATION_GENERIC_ATTEMPTS = positive_int_setting("NOTIFICATION_GENERIC_ATTEMPTS", "3")
NOTIFICATION_TERMINAL_RETENTION_HOURS = positive_int_setting("NOTIFICATION_TERMINAL_RETENTION_HOURS", "24")
OUTBOX_MAX_PENDING = positive_int_setting("OUTBOX_MAX_PENDING", "3000")
OUTBOX_ALERT_DEPTH = positive_int_setting("OUTBOX_ALERT_DEPTH", "200")

# --- БД, fanout, catch-up и метрики ---
DB_READ_POOL_SIZE = positive_int_setting("DB_READ_POOL_SIZE", "2")
FANOUT_WORKERS = positive_int_setting("FANOUT_WORKERS", "4")
FANOUT_QUEUE_SIZE = positive_int_setting("FANOUT_QUEUE_SIZE", "64")
SOURCE_SNAPSHOT_CACHE_SIZE = positive_int_setting("SOURCE_SNAPSHOT_CACHE_SIZE", "64")
SOURCE_SNAPSHOT_TTL = positive_float_setting("SOURCE_SNAPSHOT_TTL", "20")
BLACKLIST_CACHE_TTL = positive_float_setting("BLACKLIST_CACHE_TTL", "45")
FAST_SKIP_FULL_SCAN_INTERVAL = positive_float_setting("FAST_SKIP_FULL_SCAN_INTERVAL", "60")
# In-memory per-source memory of already processed item IDs (skips DB fanout when a scan brings nothing new).
SEEN_CACHE_TTL_HOURS = positive_float_setting("SEEN_CACHE_TTL_HOURS", "48")
SEEN_CACHE_MAX_PER_SOURCE = positive_int_setting("SEEN_CACHE_MAX_PER_SOURCE", "1000")
CATCHUP_MAX_AGE_MINUTES = positive_int_setting("CATCHUP_MAX_AGE_MINUTES", "30")
CATCHUP_MAX_ITEMS = positive_int_setting("CATCHUP_MAX_ITEMS", "10")
CATCHUP_MAX_PAGES = positive_int_setting("CATCHUP_MAX_PAGES", "5")
METRICS_WINDOW_SECONDS = positive_int_setting("METRICS_WINDOW_SECONDS", "3600")
METRICS_REPORT_INTERVAL = positive_int_setting("METRICS_REPORT_INTERVAL", "60")

# --- Совместимость со старыми ссылками в коде/тестах ---
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "5"))
if CHECK_INTERVAL <= 0:
    raise ValueError("Параметр CHECK_INTERVAL должен быть положительным")
MAX_CONCURRENT_WORKERS = min(32, max(1, int(os.getenv("MAX_CONCURRENT_WORKERS", "4"))))
AVITO_REQUESTS_PER_SECOND = positive_float_setting("AVITO_REQUESTS_PER_SECOND", "0.5")
AVITO_MAX_IN_FLIGHT = min(32, max(1, int(os.getenv("AVITO_MAX_IN_FLIGHT", "1"))))
AVITO_BLOCK_BASE_COOLDOWN = positive_float_setting("AVITO_BLOCK_BASE_COOLDOWN", "30")
AVITO_BLOCK_MAX_COOLDOWN = max(
    AVITO_BLOCK_BASE_COOLDOWN,
    positive_float_setting("AVITO_BLOCK_MAX_COOLDOWN", "900"),
)
AVITO_FETCH_TIMEOUT = positive_float_setting("AVITO_FETCH_TIMEOUT", str(AVITO_COLLECTION_DEADLINE))
AVITO_MAX_REQUESTS_PER_FETCH = max(1, int(os.getenv("AVITO_MAX_REQUESTS_PER_FETCH", str(AVITO_MAX_REQUESTS_PER_SCAN))))
SCAN_MAX_INTERVAL = max(float(SCAN_INTERVAL_MIN), positive_float_setting("SCAN_MAX_INTERVAL", str(SCAN_INTERVAL_MAX)))
SCAN_BUDGET_UTILIZATION = positive_float_setting("SCAN_BUDGET_UTILIZATION", "0.8")
if SCAN_BUDGET_UTILIZATION > 1:
    raise ValueError("SCAN_BUDGET_UTILIZATION must not exceed 1")
SCAN_MAX_RETRIES = min(3, max(0, int(os.getenv("SCAN_MAX_RETRIES", "2"))))
SCAN_RETRY_BASE_DELAY = positive_float_setting("SCAN_RETRY_BASE_DELAY", "5")
SCAN_RETRY_MAX_DELAY = max(SCAN_RETRY_BASE_DELAY, positive_float_setting("SCAN_RETRY_MAX_DELAY", "60"))
SCAN_STARTUP_SPREAD = positive_float_setting("SCAN_STARTUP_SPREAD", "15")
INITIAL_SNAPSHOT_TTL = positive_float_setting("INITIAL_SNAPSHOT_TTL", str(SOURCE_SNAPSHOT_TTL))


def validate_runtime_config(
    *,
    scan_interval_min: float | None = None,
    scan_interval_max: float | None = None,
    proxy_min_rate: float | None = None,
    proxy_rate_limit: float | None = None,
    proxy_rate_recommendation_max: float | None = None,
    proxy_capacity_reserve: float | None = None,
    tariff_delivery_delay: Mapping[str, float] | None = None,
    avito_connect_timeout: float | None = None,
    avito_read_timeout: float | None = None,
    avito_collection_deadline: float | None = None,
) -> None:
    s_min = SCAN_INTERVAL_MIN if scan_interval_min is None else float(scan_interval_min)
    s_max = SCAN_INTERVAL_MAX if scan_interval_max is None else float(scan_interval_max)
    p_min = PROXY_MIN_RATE if proxy_min_rate is None else float(proxy_min_rate)
    p_rate = PROXY_RATE_LIMIT if proxy_rate_limit is None else float(proxy_rate_limit)
    p_rec_max = (
        PROXY_RATE_RECOMMENDATION_MAX
        if proxy_rate_recommendation_max is None
        else float(proxy_rate_recommendation_max)
    )
    reserve = PROXY_CAPACITY_RESERVE if proxy_capacity_reserve is None else float(proxy_capacity_reserve)
    delays = TARIFF_DELIVERY_DELAY if tariff_delivery_delay is None else tariff_delivery_delay
    conn_to = AVITO_CONNECT_TIMEOUT if avito_connect_timeout is None else float(avito_connect_timeout)
    read_to = AVITO_READ_TIMEOUT if avito_read_timeout is None else float(avito_read_timeout)
    deadline = AVITO_COLLECTION_DEADLINE if avito_collection_deadline is None else float(avito_collection_deadline)

    if not math.isfinite(s_min) or s_min <= 0 or not math.isfinite(s_max) or s_max <= 0:
        raise ValueError("SCAN_INTERVAL_MIN и SCAN_INTERVAL_MAX должны быть положительными числами")
    if s_min > s_max:
        raise ValueError("Некорректная конфигурация: SCAN_INTERVAL_MIN должен быть <= SCAN_INTERVAL_MAX")

    if not (0 < p_min <= p_rate <= p_rec_max):
        raise ValueError(
            "Некорректная конфигурация скорости прокси: требуется 0 < PROXY_MIN_RATE <= PROXY_RATE_LIMIT <= PROXY_RATE_RECOMMENDATION_MAX"
        )

    if not math.isfinite(reserve) or not (0.0 <= reserve < 1.0):
        raise ValueError("Некорректная конфигурация резерва: требуется 0 <= PROXY_CAPACITY_RESERVE < 1")

    for plan_name, delay_val in delays.items():
        if not math.isfinite(float(delay_val)) or float(delay_val) < 0:
            raise ValueError(f"Некорректная тарифная задержка доставки для '{plan_name}': требуется >= 0")

    if conn_to <= 0 or read_to <= 0 or deadline <= 0:
        raise ValueError("Таймауты AVITO_CONNECT_TIMEOUT, AVITO_READ_TIMEOUT и AVITO_COLLECTION_DEADLINE должны быть > 0")
    if conn_to + read_to > deadline:
        raise ValueError(
            "Некорректная конфигурация таймаутов: AVITO_CONNECT_TIMEOUT + AVITO_READ_TIMEOUT не может превышать AVITO_COLLECTION_DEADLINE"
        )


validate_runtime_config()

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
    - socks5h://...
    """
    if not raw_proxy or not raw_proxy.strip():
        return None
    raw = raw_proxy.strip()

    scheme = "http"
    clean = raw
    if "://" in clean:
        scheme, clean = clean.split("://", 1)
    scheme = scheme.lower()

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


def tariff_delivery_delay(tariff_key: str) -> float:
    return float(TARIFF_DELIVERY_DELAY.get((tariff_key or "base").lower(), TARIFF_DELIVERY_DELAY["base"]))


# Database path
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).expanduser().resolve()
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "bot.db"))).expanduser().resolve()
