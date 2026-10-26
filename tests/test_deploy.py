"""Execute the deploy script with fake Git/systemd/Python, never touching a VPS."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _environment

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else next((str(path) for path in (Path("C:/Program Files/Git/bin/bash.exe"), Path("C:/Program Files/Git/usr/bin/bash.exe")) if path.exists()), None)
TEST_SHA = "a" * 40
OLD_SHA = "b" * 40


def bash_path(path):
    value = Path(path).resolve().as_posix()
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


@unittest.skipUnless(BASH, "Bash is needed for mocked deploy execution")
class TestDeployScript(unittest.TestCase):
    def create_script(self, directory, name, body):
        path = directory / name
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def run_deploy(self, *, fail_tests=False, fail_service=False, stale=False, posix_shell=False):
        with tempfile.TemporaryDirectory(prefix="avito-deploy-test-") as temporary:
            directory = Path(temporary)
            project = directory / "project"
            project.mkdir()
            (project / ".git").mkdir()
            (project / "venv").mkdir()
            (project / "venv" / "old-environment-marker").write_text("original", encoding="utf-8")
            (directory / "head").write_text(OLD_SHA, encoding="utf-8")
            binary = directory / "bin"
            binary.mkdir()
            self.create_script(binary, "git", """
echo "git $*" >> "$MOCK_ROOT/events"
case "$1" in
  diff|fetch|cat-file|merge-base) exit 0 ;;
  symbolic-ref) echo feature/json-api ;;
  rev-parse)
    if [[ "$2" == FETCH_HEAD ]]; then
      if [[ "$STALE" == 1 ]]; then echo "$OLD_SHA"; else echo "$DEPLOY_SHA"; fi
    else cat "$MOCK_ROOT/head"; fi ;;
  archive) exit 0 ;;
  checkout)
    for arg in "$@"; do last="$arg"; done
    echo "$last" > "$MOCK_ROOT/head" ;;
  *) echo "Unexpected git command" >&2; exit 91 ;;
esac
""")
            self.create_script(binary, "systemctl", """
echo "systemctl $*" >> "$MOCK_ROOT/events"
case "$1" in
  is-active)
    if [[ -f "$MOCK_ROOT/new-start" && "$FAIL_SERVICE" == 1 ]]; then exit 1; fi ;;
  stop) rm -f "$MOCK_ROOT/new-start" ;;
  reset-failed) true ;;
  start)
    if [[ "$(cat "$MOCK_ROOT/head")" == "$DEPLOY_SHA" ]]; then touch "$MOCK_ROOT/new-start"; fi ;;
  show) echo 0 ;;
  *) exit 92 ;;
esac
""")
            self.create_script(binary, "tar", "cat >/dev/null\n")
            self.create_script(binary, "flock", "exit 0\n")
            self.create_script(binary, "sleep", "exit 0\n")
            # A regular link marker avoids Git Bash's Windows directory-copy fallback.
            # Fake systemd never executes venv; rollback only needs unlink/move semantics.
            self.create_script(binary, "ln", "printf '%s\\n' \"$2\" > \"$3\"\n")
            self.create_script(binary, "timeout", "shift\nexec \"$@\"\n")
            self.create_script(binary, "python3.11", """
echo "build-env $*" >> "$MOCK_ROOT/events"
mkdir -p "$3/bin"
cp "$MOCK_ROOT/bin/python-template" "$3/bin/python"
chmod +x "$3/bin/python"
""")
            self.create_script(binary, "python-template", """
echo "python $*" >> "$MOCK_ROOT/events"
if [[ "$*" == *run_offline_tests.py* && "$FAIL_TESTS" == 1 ]]; then exit 17; fi
if [[ "$*" == "-B -" ]]; then cat >/dev/null; echo backup-created >> "$MOCK_ROOT/events"; fi
""")
            prefix = bash_path(binary)
            env = dict(os.environ)
            env.update({
                "MOCK_ROOT": bash_path(directory), "DEPLOY_TARGET_DIR": bash_path(project),
                "DEPLOY_SHA": TEST_SHA, "OLD_SHA": OLD_SHA, "DEPLOY_BRANCH": "feature/json-api",
                "FAIL_TESTS": "1" if fail_tests else "0", "FAIL_SERVICE": "1" if fail_service else "0",
                "STALE": "1" if stale else "0",
            })
            if posix_shell:
                wrapper = directory / "wrapper.sh"
                wrapper.write_text("bash -s <<'AVITO_DEPLOY_SCRIPT'\n" + (ROOT / "tools/deploy.sh").read_text(encoding="utf-8") + "\nAVITO_DEPLOY_SCRIPT\n", encoding="utf-8", newline="\n")
                command = f"export PATH='{prefix}':$PATH; sh '{bash_path(wrapper)}'"
            else:
                command = f"export PATH='{prefix}':$PATH; bash '{bash_path(ROOT / 'tools/deploy.sh')}'"
            result = subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True, timeout=15)
            head = (directory / "head").read_text(encoding="utf-8").strip()
            events_path = directory / "events"
            events = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
            old_environment = (project / "venv" / "old-environment-marker").is_file()
            return result, head, events, old_environment

    def test_success_deploys_exact_sha_after_tests_and_backup(self):
        result, head, events, old_environment = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertFalse(old_environment)
        self.assertLess(events.index("run_offline_tests.py"), events.index("systemctl stop"))
        self.assertLess(events.index("backup-created"), events.index("git checkout"))
        self.assertEqual(events.count("systemctl show"), 10)

    def test_test_failure_never_stops_running_bot(self):
        result, head, events, old_environment = self.run_deploy(fail_tests=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertNotIn("systemctl stop", events)
        self.assertNotIn("git checkout", events)

    def test_failed_service_restores_code_and_old_environment(self):
        result, head, events, old_environment = self.run_deploy(fail_service=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment, result.stdout + result.stderr + events)
        self.assertIn(f"git checkout -B feature/json-api {OLD_SHA}", events)

    def test_stale_workflow_skips_deploy(self):
        result, head, events, old_environment = self.run_deploy(stale=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, OLD_SHA)
        self.assertTrue(old_environment)
        self.assertNotIn("build-env", events)
        self.assertNotIn("systemctl stop", events)

    def test_ssh_wrapper_runs_under_a_posix_login_shell(self):
        result, head, events, old_environment = self.run_deploy(posix_shell=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(head, TEST_SHA)
        self.assertIn("backup-created", events)


if __name__ == "__main__":
    unittest.main()