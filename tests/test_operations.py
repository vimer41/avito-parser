import os
import tempfile
import time
import unittest
from pathlib import Path

from tests import _environment
from tools.cleanup_releases import cleanup, cleanup_plan
from tools.configure_stars import configure
from tools.diagnose import scan_summary


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="avito-retention-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.releases = self.root / ".releases"
        self.backups = self.root / "backups"
        self.releases.mkdir()
        self.backups.mkdir()
        self.now = time.time()

    def release(self, number, age=30):
        path = self.releases / f"venv-{number:040x}-abcdef"
        path.mkdir()
        (path / "marker").write_text("environment", encoding="utf-8")
        os.utime(path, (self.now - age * 86400 - number, self.now - age * 86400 - number))
        return path

    def backup(self, number, age=30):
        path = self.backups / f"bot-before-{number:040x}-20261026T000000Z.db"
        path.write_bytes(b"fixture database")
        os.utime(path, (self.now - age * 86400 - number, self.now - age * 86400 - number))
        return path

    def symlink(self, path, target):
        try:
            path.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit directory symlinks")

    def test_count_age_and_explicit_active_rollback_backup_pins(self):
        releases = [self.release(i) for i in range(8)]
        backups = [self.backup(i) for i in range(12)]
        recent = self.release(20, age=1)
        plan = cleanup_plan(self.root, keep_releases=2, keep_backups=3,
                            protect=[releases[-1], releases[-2], backups[-1]], now=self.now)
        self.assertNotIn(recent, plan)
        self.assertNotIn(releases[-1], plan)
        self.assertNotIn(releases[-2], plan)
        self.assertNotIn(backups[-1], plan)
        self.assertIn(releases[4], plan)
        self.assertIn(backups[5], plan)
        self.assertNotIn(backups[0], plan)

    def test_default_is_dry_run_and_apply_only_removes_known_expired_names(self):
        [self.release(i) for i in range(7)]
        [self.backup(i) for i in range(10)]
        unknown = self.releases / "manual-environment"
        unknown.mkdir()
        secret = self.root / ".env"
        secret.write_text("SECRET=never-delete", encoding="utf-8")
        data = self.root / "data"
        data.mkdir()
        (data / "bot.db").write_bytes(b"production")
        plan = cleanup(self.root, now=self.now)
        self.assertTrue(plan)
        self.assertTrue(all(path.exists() for path in plan))
        applied = cleanup(self.root, apply=True, now=self.now)
        self.assertEqual(applied, plan)
        self.assertFalse(any(path.exists() for path in plan))
        self.assertTrue(unknown.exists())
        self.assertEqual(secret.read_text(), "SECRET=never-delete")
        self.assertEqual((data / "bot.db").read_bytes(), b"production")

    def test_active_symlink_and_rollback_alias_pin_old_target(self):
        old = self.release(100)
        rollback = self.release(101)
        [self.release(i) for i in range(7)]
        self.symlink(self.root / "venv", old)
        alias = self.releases / "previous-venv-20260901T000000Z"
        self.symlink(alias, rollback)
        plan = cleanup_plan(self.root, now=self.now)
        self.assertNotIn(old, plan)
        self.assertNotIn(rollback, plan)
        self.assertNotIn(alias, plan)

    def test_external_links_are_unlinked_never_followed(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        sentinel = outside / "must-survive"
        sentinel.write_bytes(b"protected")
        [self.release(i, age=1) for i in range(6)]
        alias = self.releases / "previous-venv-20260101T000000Z"
        self.symlink(alias, outside)
        newer = self.releases / "previous-venv-20261026T000000Z"
        self.symlink(newer, self.releases / f"venv-{0:040x}-abcdef")
        try:
            os.utime(alias, (self.now - 90 * 86400, self.now - 90 * 86400), follow_symlinks=False)
        except (OSError, NotImplementedError):
            self.skipTest("OS cannot set symlink timestamps")
        cleanup(self.root, apply=True, now=self.now)
        self.assertEqual(sentinel.read_bytes(), b"protected")

    def test_unsafe_roots_directories_and_retention_values_are_rejected(self):
        for options in ({"keep_releases": 1}, {"keep_backups": 2}, {"release_age_days": 0}, {"backup_age_days": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                cleanup_plan(self.root, **options)
        with self.assertRaises(ValueError):
            cleanup_plan(Path(self.directory.name))
        self.backups.rmdir()
        self.symlink(self.backups, Path(self.directory.name))
        with self.assertRaises(ValueError):
            cleanup_plan(self.root)


class TestStarsRuntimeConfig(unittest.TestCase):
    def test_atomic_update_preserves_all_secret_and_unrelated_values(self):
        with tempfile.TemporaryDirectory(prefix="avito-runtime-config-") as temporary:
            path = Path(temporary) / ".env"
            path.write_text("BOT_TOKEN=fixture-secret\n# retained comment\nCHECK_INTERVAL=25\n"
                            "STARS_PAYMENTS_ENABLED=0\nexport STARS_PRICE_PRO=999\nSTARS_PRICE_PRO=999\n", encoding="utf-8")
            configure(path, enabled="1", standart=170, pro=470)
            text = path.read_text(encoding="utf-8")
            self.assertIn("BOT_TOKEN=fixture-secret\n", text)
            self.assertIn("# retained comment\nCHECK_INTERVAL=25\n", text)
            self.assertEqual(text.count("STARS_PRICE_PRO="), 1)
            self.assertIn("STARS_PAYMENTS_ENABLED=1", text)
            self.assertIn("STARS_PRICE_STANDART=170", text)
            self.assertIn("STARS_PRICE_PRO=470", text)
            self.assertEqual(list(Path(temporary).glob(".stars-config-*")), [])

    def test_invalid_prices_do_not_change_runtime_file(self):
        with tempfile.TemporaryDirectory(prefix="avito-runtime-config-") as temporary:
            path = Path(temporary) / ".env"
            path.write_text("BOT_TOKEN=fixture", encoding="utf-8")
            for options in ({"enabled": "yes", "standart": 170, "pro": 470},
                            {"enabled": "1", "standart": 0, "pro": 470},
                            {"enabled": "1", "standart": 470, "pro": 170}):
                with self.assertRaises(ValueError):
                    configure(path, **options)
                self.assertEqual(path.read_text(), "BOT_TOKEN=fixture")


class TestDiagnosticMetrics(unittest.TestCase):
    def test_scan_counts_latencies_and_safe_output(self):
        raw = "\n".join([
            "secret-proxy-url=http://user:password@localhost", "user-private-text=fixture",
            "Scan result: ok=1 elapsed_ms=100 items=10 blocked=0",
            "Scan result: ok=1 elapsed_ms=300 items=5 blocked=0",
            "Scan result: ok=0 elapsed_ms=500 items=0 blocked=1",
            "HTTP 429", "Live Telegram probe completed by admin=123456",
        ])
        result = scan_summary(raw)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["successes"], 2)
        self.assertEqual(result["blocked_attempts"], 1)
        self.assertEqual(result["success_rate"], .6667)
        self.assertEqual(result["success_latency_p95_ms"], 300)
        self.assertEqual(result["http_429_entries"], 1)
        self.assertEqual(result["real_admin_probe_entries"], 1)
        self.assertNotIn("password", str(result))
        self.assertNotIn("private-text", str(result))

    def test_no_scan_entries_do_not_claim_success(self):
        result = scan_summary("Starting process without any scans yet")
        self.assertEqual(result["attempts"], 0)
        self.assertIsNone(result["success_rate"])
        self.assertIsNone(result["success_latency_p50_ms"])