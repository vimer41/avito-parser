"""Conservative deployment retention. Dry-run unless --apply is explicitly given."""

import argparse
import re
import shutil
import stat
import time
from pathlib import Path

RELEASE_NAME = re.compile(r"(?:venv-[0-9a-f]{40}-[A-Za-z0-9]{6,}|previous-venv-\d{8}T\d{6}Z)")
BACKUP_NAME = re.compile(r"bot-before-[0-9a-f]{40}-\d{8}T\d{6}Z\.db")


def is_link(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


def cleanup_plan(target: Path, *, keep_releases: int = 4, keep_backups: int = 7,
                 release_age_days: int = 7, backup_age_days: int = 14,
                 protect=(), now: float | None = None) -> list[Path]:
    if keep_releases < 2 or keep_backups < 3 or release_age_days < 1 or backup_age_days < 1:
        raise ValueError("Retention requires >=2 releases, >=3 backups and ages >=1 day")
    target = target.resolve(strict=True)
    if target == Path(target.anchor) or not (target / ".git").is_dir():
        raise ValueError("Expected a project directory, not a filesystem root")
    releases, backups = target / ".releases", target / "backups"
    for directory in (releases, backups):
        if directory.exists() or directory.is_symlink():
            if is_link(directory) or not directory.is_dir():
                raise ValueError("Retention directories must be real directories inside the project")
    now = time.time() if now is None else now
    protected = {Path(path).absolute() for path in protect}
    protected.update(Path(path).resolve() for path in protect)
    active = target / "venv"
    if active.exists() or active.is_symlink():
        protected.add(active.resolve())

    def candidates(directory, pattern, directories):
        if not directory.is_dir():
            return []
        matching = []
        for path in directory.iterdir():
            if not pattern.fullmatch(path.name):
                continue
            link = is_link(path)
            if (directories and (link or path.is_dir())) or (not directories and not link and path.is_file()):
                matching.append(path)
        return sorted(matching,
                      key=lambda path: (path.lstat().st_mtime, path.name), reverse=True)

    release_paths = candidates(releases, RELEASE_NAME, True)
    backup_paths = candidates(backups, BACKUP_NAME, False)
    # Symlink aliases are not separate environments and must not consume the
    # minimum count of real environments retained for rollback.
    protected.update([path for path in release_paths if not is_link(path)][:keep_releases])
    protected.update(backup_paths[:keep_backups])
    previous = [path for path in release_paths if path.name.startswith("previous-venv-")]
    if previous:
        protected.add(previous[0])
    # An alias kept for rollback pins its environment. Never rmtree through links.
    for path in release_paths:
        if path in protected or path.lstat().st_mtime >= now - release_age_days * 86400:
            protected.add(path)
            if is_link(path):
                protected.add(path.resolve())
    delete = []
    for paths, age in ((release_paths, release_age_days), (backup_paths, backup_age_days)):
        for path in paths:
            if path not in protected and path.lstat().st_mtime < now - age * 86400:
                delete.append(path)
    return delete


def cleanup(target: Path, *, apply: bool = False, **options) -> list[Path]:
    plan = cleanup_plan(target, **options)
    for path in plan:
        print(f"Retention {'remove' if apply else 'would remove'}: {path.name}", flush=True)
        if apply:
            if is_link(path) or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--keep-releases", type=int, default=4)
    parser.add_argument("--keep-backups", type=int, default=7)
    parser.add_argument("--release-age-days", type=int, default=7)
    parser.add_argument("--backup-age-days", type=int, default=14)
    parser.add_argument("--protect", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = vars(parser.parse_args())
    cleanup(**args)


if __name__ == "__main__":
    main()