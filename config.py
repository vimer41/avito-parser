import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MAX_SEARCHES_PER_USER = int(os.getenv("MAX_SEARCHES_PER_USER", "10"))
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
# Список администраторов бота (Захар @vimerd и Арсений @prostatock)
ADMIN_IDS = [986832959, 455386428]
env_admin = os.getenv("ADMIN_ID", "986832959")
try:
    for aid in env_admin.split(","):
        if aid.strip() and int(aid.strip()) not in ADMIN_IDS:
            ADMIN_IDS.append(int(aid.strip()))
except Exception:
    pass
ADMIN_ID = ADMIN_IDS[0]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# Database path
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"
