"""Atomically change only approved, non-secret Stars settings in a runtime .env."""

import argparse
import os
import re
import tempfile
from pathlib import Path

KEY = re.compile(r"^\s*(?:export\s+)?(STARS_PAYMENTS_ENABLED|STARS_PRICE_STANDART|STARS_PRICE_PRO)\s*=")


def configure(env_file: Path, *, enabled: str, standart: int, pro: int):
    if enabled not in ("0", "1") or standart <= 0 or pro <= standart:
        raise ValueError("Expected enabled=0/1 and positive Stars prices, PRO > Standart")
    if env_file.is_symlink():
        raise ValueError("Refusing to replace a symlinked .env")
    original = env_file.read_text(encoding="utf-8-sig") if env_file.exists() else ""
    lines = [line for line in original.splitlines() if not KEY.match(line)]
    lines.extend([f"STARS_PAYMENTS_ENABLED={enabled}", f"STARS_PRICE_STANDART={standart}", f"STARS_PRICE_PRO={pro}"])
    env_file.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".stars-config-", dir=env_file.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines) + "\n")
        os.chmod(name, 0o600)
        os.replace(name, env_file)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f"Stars runtime configuration: enabled={enabled} standart={standart} pro={pro}; secrets unchanged")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--enabled", choices=("0", "1"), required=True)
    parser.add_argument("--standart", type=int, required=True)
    parser.add_argument("--pro", type=int, required=True)
    arguments = vars(parser.parse_args())
    configure(**arguments)