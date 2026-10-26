"""Shared test bootstrap: no .env, production SQLite, cookies or logs."""

import atexit
import os
import tempfile
from pathlib import Path

TEST_ROOT = tempfile.TemporaryDirectory(prefix="avito-radar-tests-")
atexit.register(TEST_ROOT.cleanup)
root = Path(TEST_ROOT.name)
os.environ.update({
    "AVITO_RADAR_TESTING": "1",
    "BOT_TOKEN": "1234567890:TEST_TOKEN_FOR_CI_CD_MOCK",
    "ADMIN_ID": "986832959",
    "ADMIN_IDS": "986832959,455386428",
    "DATA_DIR": str(root / "data"),
    "DB_PATH": str(root / "test.db"),
    "LOGS_DIR": str(root / "logs"),
    "COMBINED_LOG": str(root / "bot.log"),
    "PROXY_URL": "",
})