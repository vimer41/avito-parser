"""Compile source in memory: no pyc files, .env imports or runtime startup."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
files = list(ROOT.glob("*.py"))
for directory in ("anti_bot", "handlers", "tests", "tools"):
    files.extend((ROOT / directory).rglob("*.py"))
for source in sorted(files):
    compile(source.read_text(encoding="utf-8-sig"), str(source), "exec")
print(f"Syntax OK: {len(files)} Python files")