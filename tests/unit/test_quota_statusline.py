"""Claude statusLine quota writer and advisory notification regressions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.helpers import TempDir, git, hook, install, run_script, write_json

import quota_statusline
from _lib import quota_notice


NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def payload(*, requests=1, five=23.5, weekly=41.2, session="session-live"):
    return {
        "session_id": session,
        "prompt_cache": {"requests": requests},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": 1793901600},
            "seven_day": {"used_percentage": weekly, "resets_at": 1794506400},
        },
    }


class StatusLineWriterTests(unittest.TestCase):
    def test_documented_payload_writes_atomic_engine_shape(self):
        with TempDir() as root, TempDir() as cache:
            self.assertTrue(quota_statusline.update(root, payload(), cache_root=cache, now=NOW))
            data = json.loads((root / "docs/_cgspec/07-state/quota.json").read_text())
            self.assertEqual(data, {
                "schema": 1,
                "read_at": "2026-11-05T12:00:00Z",
                "source": "claude-statusline",
                "five_hour": {"used_pct": 23.5, "resets_at": "2026-11-05T18:00:00Z"},
                "weekly": {"used_pct": 41.2, "resets_at": "2026-11-12T18:00:00Z"},
            })
            self.assertEqual(list((root / "docs/_cgspec/07-state").glob(".quota.json.*.tmp")), [])

    def test_same_or_lower_request_counter_never_refreshes_read_at(self):
        with TempDir() as root, TempDir() as cache:
            self.assertTrue(quota_statusline.update(root, payload(requests=7), cache_root=cache, now=NOW))
            before = (root / "docs/_cgspec/07-state/quota.json").read_bytes()
            later = datetime(2026, 11, 5, 12, 4, tzinfo=timezone.utc)
            self.assertFalse(quota_statusline.update(root, payload(requests=7), cache_root=cache, now=later))
            self.assertFalse(quota_statusline.update(root, payload(requests=6), cache_root=cache, now=later))
            self.assertEqual((root / "docs/_cgspec/07-state/quota.json").read_bytes(), before)

    def test_counter_is_isolated_per_session(self):
        with TempDir() as root, TempDir() as cache:
            self.assertTrue(quota_statusline.update(root, payload(requests=3, session="a"), cache_root=cache, now=NOW))
            self.assertTrue(quota_statusline.update(root, payload(requests=1, session="b"), cache_root=cache, now=NOW))

    def test_invalid_payloads_or_no_usable_window_write_nothing_and_never_raise(self):
        invalid = [
            None,
            {},
            {"session_id": "s", "prompt_cache": {"requests": 1}},
            payload(requests=True),
        ]
        for source, window in (
            ("five_hour", {"used_percentage": -1, "resets_at": 1793901600}),
            ("seven_day", {"used_percentage": 101, "resets_at": 1794506400}),
            ("five_hour", {"used_percentage": float("nan"), "resets_at": 1793901600}),
            ("five_hour", {"used_percentage": 20, "resets_at": "tomorrow"}),
            ("seven_day", {"used_percentage": 20}),
        ):
            broken = payload()
            broken["rate_limits"] = {source: window}
            invalid.append(broken)
        invalid.append(payload())
        invalid[-1]["rate_limits"] = {}
        for index, value in enumerate(invalid):
            with self.subTest(index=index), TempDir() as root, TempDir() as cache:
                self.assertFalse(quota_statusline.update(root, value, cache_root=cache, now=NOW))
                self.assertFalse((root / "docs/_cgspec/07-state/quota.json").exists())

    def test_one_missing_window_writes_the_real_window_and_evaluator_stays_bounded(self):
        with TempDir() as root, TempDir() as cache:
            one = payload()
            del one["rate_limits"]["five_hour"]
            self.assertTrue(quota_statusline.update(root, one, cache_root=cache, now=NOW))
            data = json.loads((root / "docs/_cgspec/07-state/quota.json").read_text())
            self.assertNotIn("five_hour", data)
            from _lib.quota import evaluate
            self.assertEqual(evaluate(data, now=NOW)["mode"], "bounded")

    def test_expired_window_does_not_hide_another_windows_emergency(self):
        with TempDir() as root, TempDir() as cache:
            mixed = payload(weekly=97)
            mixed["rate_limits"]["five_hour"]["resets_at"] = int(NOW.timestamp())
            self.assertTrue(quota_statusline.update(root, mixed, cache_root=cache, now=NOW))
            data = json.loads((root / "docs/_cgspec/07-state/quota.json").read_text())
            self.assertNotIn("five_hour", data)
            from _lib.quota import evaluate
            self.assertEqual(evaluate(data, now=NOW)["mode"], "emergency")

    def test_cli_malformed_input_and_unwritable_root_exit_zero(self):
        with TempDir() as root, TempDir() as cache:
            env = dict(os.environ, XDG_CACHE_HOME=str(cache))
            result = run_script("quota_statusline.py", "--root", str(root), stdin="not json", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_chained_command_gets_identical_stdin_and_stdout_is_preserved(self):
        with TempDir() as root, TempDir() as cache:
            chain = f"{sys.executable} -c 'import sys; print(sys.stdin.read(), end=\"\")'"
            raw = json.dumps(payload())
            env = dict(os.environ, XDG_CACHE_HOME=str(cache))
            result = run_script("quota_statusline.py", "--root", str(root), "--chain", chain,
                                stdin=raw, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, raw)


class QuotaNoticeTests(unittest.TestCase):
    def test_session_start_reports_configured_stale_and_healthy_states(self):
        with TempDir() as root:
            install(root)
            missing = hook(root, "session-start", {"session_id": "s"}, "--tool", "claude")
            self.assertIn("configured but no fresh complete provider reading", missing.stdout)
            write_json(root / "docs/_cgspec/07-state/quota.json", {
                "schema": 1, "source": "claude-statusline", "read_at": "2000-01-01T00:00:00Z",
                "five_hour": {"used_pct": 20}, "weekly": {"used_pct": 20},
            })
            stale = hook(root, "session-start", {"session_id": "s"}, "--tool", "claude")
            self.assertRegex(stale.stdout, r"stale quota reading \(\d+d old\)")
            self.assertIn("configured but no fresh complete provider reading", stale.stdout)
            write_json(root / "docs/_cgspec/07-state/quota.json", {
                "schema": 1, "source": "claude-statusline", "read_at": datetime.now(timezone.utc).isoformat(),
                "five_hour": {"used_pct": 20}, "weekly": {"used_pct": 20},
            })
            healthy = hook(root, "session-start", {"session_id": "s"}, "--tool", "claude")
            self.assertIn("quota supervision: healthy", healthy.stdout)

    def test_session_start_reports_missing_collector_without_blocking(self):
        with TempDir() as root:
            install(root)
            settings = root / ".claude/settings.json"
            data = json.loads(settings.read_text())
            data.pop("statusLine")
            write_json(settings, data)
            result = hook(root, "session-start", {"session_id": "s"}, "--tool", "claude")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("collector is not configured", result.stdout)

    def test_session_start_detects_higher_precedence_local_override(self):
        overrides = [
            "printf local",
            ["printf", "local"],
            {"type": "http", "url": "https://example.invalid/status"},
            {"type": "command", "command": "printf local"},
        ]
        for override in overrides:
            with self.subTest(override=override), TempDir() as root:
                install(root)
                write_json(root / ".claude/settings.local.json", {"statusLine": override})
                result = hook(root, "session-start", {"session_id": "s"}, "--tool", "claude")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("collector is not configured", result.stdout)

    def test_mode_changes_and_five_minute_repeats_use_monotonic_time(self):
        with TempDir() as root, TempDir() as cache:
            first = quota_notice.next_notice(root, "s", cache_root=cache, monotonic_now=100)
            self.assertIn("unavailable", first)
            self.assertIsNone(quota_notice.next_notice(root, "s", cache_root=cache, monotonic_now=399))
            self.assertIn("unavailable", quota_notice.next_notice(root, "s", cache_root=cache,
                                                                  monotonic_now=400))
            write_json(root / "docs/_cgspec/07-state/quota.json", {
                "schema": 1, "source": "claude-statusline", "read_at": datetime.now(timezone.utc).isoformat(),
                "five_hour": {"used_pct": 20}, "weekly": {"used_pct": 20},
            })
            recovered = quota_notice.next_notice(root, "s", cache_root=cache, monotonic_now=401)
            self.assertIn("recovered", recovered)

    def test_manual_snapshot_does_not_claim_automatic_claude_supervision(self):
        with TempDir() as root, TempDir() as cache:
            write_json(root / "docs/_cgspec/07-state/quota.json", {
                "schema": 1, "source": "manual", "read_at": datetime.now(timezone.utc).isoformat(),
                "five_hour": {"used_pct": 20}, "weekly": {"used_pct": 20},
            })
            notice = quota_notice.next_notice(root, "s", cache_root=cache, monotonic_now=1)
            self.assertIn("unavailable", notice)

    def test_lowest_threshold_repeats_without_blocking_post_tool(self):
        with TempDir() as root, TempDir() as cache:
            install(root, env=dict(os.environ, XDG_CACHE_HOME=str(cache)))
            write_json(root / "docs/_cgspec/07-state/quota.json", {
                "schema": 1, "source": "claude-statusline", "read_at": datetime.now(timezone.utc).isoformat(),
                "five_hour": {"used_pct": 96}, "weekly": {"used_pct": 50},
            })
            env = dict(os.environ, XDG_CACHE_HOME=str(cache))
            result = hook(root, "post-tool", {"session_id": "s", "tool_name": "Read"},
                          "--tool", "claude", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            message = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("no new work", message)


class QuotaInstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_install_wires_event_only_statusline_and_gitignores_snapshot(self):
        result = install(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = json.loads((self.root / ".claude/settings.json").read_text())
        status = settings["statusLine"]
        self.assertEqual(status["type"], "command")
        self.assertIn("quota_statusline.py", status["command"])
        self.assertNotIn("refreshInterval", status)
        self.assertIn("docs/_cgspec/07-state/quota.json", (self.root / ".gitignore").read_text().splitlines())

    def test_existing_statusline_is_chained_then_restored_on_uninstall(self):
        (self.root / ".claude").mkdir()
        previous = {"type": "command", "command": "printf mine", "padding": 2, "refreshInterval": 17}
        (self.root / ".claude/settings.json").write_text(json.dumps({"statusLine": previous}))
        self.assertEqual(install(self.root).returncode, 0)
        installed = json.loads((self.root / ".claude/settings.json").read_text())["statusLine"]
        self.assertIn("quota_statusline.py", installed["command"])
        self.assertIn("--chain", installed["command"])
        self.assertNotIn("refreshInterval", installed)
        self.assertEqual(run_script("install.py", "uninstall", "--root", str(self.root)).returncode, 0)
        self.assertEqual(json.loads((self.root / ".claude/settings.json").read_text())["statusLine"], previous)

    def test_user_scope_statusline_is_dynamically_chained_without_copying_its_command(self):
        with TempDir() as config:
            user_command = f"{sys.executable} -c 'import sys; print(\"user-line\")'"
            write_json(config / "settings.json", {"statusLine": {
                "type": "command", "command": user_command, "padding": 3,
            }})
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config))
            result = install(self.root, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("dynamically chained the user-scope command", result.stdout)
            installed = json.loads((self.root / ".claude/settings.json").read_text())["statusLine"]
            self.assertIn("--chain-user-statusline", installed["command"])
            self.assertNotIn(user_command, installed["command"])
            self.assertEqual(installed["padding"], 3)
            called = subprocess.run(installed["command"], shell=True, cwd=self.root, input="{}",
                                    capture_output=True, text=True, env=env)
            self.assertEqual(called.returncode, 0, called.stderr)
            self.assertEqual(called.stdout, "user-line\n")

    def test_non_command_statusline_is_left_untouched_with_a_note(self):
        (self.root / ".claude").mkdir()
        previous = {"type": "http", "url": "https://example.invalid/status"}
        (self.root / ".claude/settings.json").write_text(json.dumps({"statusLine": previous}))
        result = install(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("left existing Claude statusLine alone", result.stdout)
        self.assertEqual(json.loads((self.root / ".claude/settings.json").read_text())["statusLine"], previous)

    def test_upgrade_untracks_existing_quota_snapshot_without_deleting_it(self):
        self.assertEqual(install(self.root).returncode, 0)
        quota = self.root / "docs/_cgspec/07-state/quota.json"
        write_json(quota, {"schema": 1, "source": "manual", "read_at": "2026-11-05T00:00:00Z"})
        git(self.root, "add", "-f", quota.relative_to(self.root).as_posix())
        git(self.root, "commit", "-q", "-m", "track old quota")
        result = run_script("install.py", "upgrade", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(quota.exists())
        self.assertEqual(git(self.root, "ls-files", quota.relative_to(self.root).as_posix()).stdout, "")


if __name__ == "__main__":
    unittest.main()
