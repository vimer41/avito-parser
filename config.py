import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
TESTING = os.getenv("AVITO_RADAR_TESTING") == "1"
if not TESTING:
    load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "25"))
MAX_SEARCHES_PER_USER = int(os.getenv("MAX_SEARCHES_PER_USER", "20"))
# Количество параллельных воркеров мониторинга (по умолчанию 30)
MAX_CONCURRENT_WORKERS = int(os.getenv("MAX_CONCURRENT_WORKERS", "30"))
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
