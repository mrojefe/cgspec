"""Quota safety: missing, old or malformed readings must never look available."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from tests.helpers import TempDir, install, run_script, write_json
from _lib.quota import evaluate


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def reading(**changes):
    data = {"schema": 1, "source": "manual", "read_at": "2026-10-20T11:59:00Z",
            "five_hour": {"used_pct": 40}, "weekly": {"used_pct": 50}}
    data.update(changes)
    return data


class QuotaTests(unittest.TestCase):
    def test_fresh_available_is_distinct_from_context_capacity(self):
        result = evaluate(reading(), now=NOW)
        self.assertEqual(result["mode"], "normal")
        self.assertEqual(result["remaining_pct"], 50)
        self.assertIn("context", result["context"])

    def test_either_window_at_fifteen_percent_remaining_requests_checkpoint(self):
        for window in ("five_hour", "weekly"):
            with self.subTest(window=window):
                result = evaluate(reading(**{window: {"used_pct": 85}}), now=NOW)
                self.assertEqual(result["mode"], "checkpoint")

    def test_invalid_missing_stale_and_future_readings_are_bounded(self):
        cases = [None, [], {}, reading(source="unavailable"), reading(source="invented"),
                 reading(read_at="yesterday"), reading(read_at="2026-10-20T11:59:00"),
                 reading(read_at="2026-10-20T11:54:59Z"),
                 reading(read_at="2026-10-20T12:01:00Z"),
                 reading(five_hour={"used_pct": 40, "resets_at": "not a date"}),
                 reading(five_hour={"used_pct": True}), reading(weekly={"used_pct": -1}),
                 reading(five_hour={"used_pct": float("nan")}),
                 reading(weekly={"used_pct": 101}),
                 reading(five_hour={"used_pct": 99, "resets_at": "2026-10-20T11:59:30Z"})]
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(evaluate(data, now=NOW)["mode"], "bounded")

    def test_no_usable_window_is_bounded(self):
        self.assertEqual(evaluate(reading(five_hour={}, weekly={}), now=NOW)["mode"], "bounded")

    def test_single_known_window_cannot_imply_complete_quota(self):
        data = reading()
        del data["weekly"]
        self.assertEqual(evaluate(data, now=NOW)["mode"], "bounded")
        data["five_hour"]["used_pct"] = 90
        self.assertEqual(evaluate(data, now=NOW)["mode"], "checkpoint")

    def test_exact_freshness_boundary_and_offset_time(self):
        self.assertEqual(evaluate(reading(read_at="2026-10-20T11:55:00+00:00"), now=NOW)["mode"], "normal")
        self.assertEqual(evaluate(reading(read_at="2026-10-20T11:59:00.123Z"), now=NOW)["mode"], "normal")

    def test_weekly_resume_is_separate_from_scheduling(self):
        for used, advice in ((89, "quota-permits"), (90, "blocked")):
            result = evaluate(reading(weekly={"used_pct": used}), now=NOW)
            self.assertEqual(result["weekly_resume"], advice)
            self.assertEqual(result["windows"]["weekly"], 100-used)

    def test_bad_window_does_not_hide_another_windows_emergency(self):
        for broken in ({"used_pct": 40, "resets_at": "garbage"},
                       {"used_pct": 40, "resets_at": "2026-10-20T13:00:00"},
                       {"used_pct": True}, {"used_pct": 101}, []):
            for used, expected in ((97, "emergency"), (88, "checkpoint"), (40, "bounded")):
                with self.subTest(broken=broken, used=used):
                    data = reading(five_hour={"used_pct": used}, weekly=broken)
                    self.assertEqual(evaluate(data, now=NOW)["mode"], expected)

    def test_frugal_and_emergency_thresholds(self):
        for used, mode in ((74, "normal"), (75, "frugal"), (84, "frugal"),
                           (85, "checkpoint"), (94, "checkpoint"), (95, "emergency"), (100, "emergency")):
            with self.subTest(used=used):
                self.assertEqual(evaluate(reading(weekly={"used_pct": used}), now=NOW)["mode"], mode)

    def test_cli_and_session_pack_do_not_present_stale_numbers_as_current(self):
        with TempDir() as root:
            install(root)
            path = root / "docs/_cgspec/07-state/quota.json"
            write_json(path, reading(read_at="2000-01-01T00:00:00Z"))
            out = run_script("quota.py", "--root", str(root), "--json")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["mode"], "bounded")
            pack = run_script("cgspec_hook.py", "session-start", "--root", str(root))
            self.assertIn("stale", pack.stdout)
            self.assertNotIn("60%", pack.stdout)
            self.assertNotIn("50%", pack.stdout)
            self.assertNotIn("quota: normal", pack.stdout)
            self.assertIn("bounded", pack.stdout)

    def test_fresh_reading_reaches_session_pack(self):
        with TempDir() as root:
            install(root)
            data = reading(read_at=datetime.now(timezone.utc).isoformat())
            write_json(root / "docs/_cgspec/07-state/quota.json", data)
            pack = run_script("cgspec_hook.py", "session-start", "--root", str(root))
            self.assertIn("quota: normal", pack.stdout)
            self.assertIn("five_hour 60% remaining", pack.stdout)
            self.assertIn("source manual", pack.stdout)

    def test_multimodel_matrix_and_subagent_preflight(self):
        from _lib.quota import evaluate_subagent_preflight, evaluate_assistant_prompt
        data = reading(
            source="antigravity",
            models={
                "gemini-argon": {"used_pct": 100, "status": "exhausted", "resets_at": "2026-10-20T14:00:00Z"},
                "claude-sonnet": {"used_pct": 0, "status": "nominal"}
            },
            default_fallback={"gemini-argon": "claude-sonnet"}
        )
        # Check subagent preflight for exhausted model
        pre_exhausted = evaluate_subagent_preflight(data, "gemini-argon", now=NOW)
        self.assertFalse(pre_exhausted["allowed"])
        self.assertEqual(pre_exhausted["decision"], "deny")
        self.assertEqual(pre_exhausted["fallback_model"], "claude-sonnet")
        self.assertIn("exhausted", pre_exhausted["reason"])

        # Check subagent preflight for available model
        pre_ok = evaluate_subagent_preflight(data, "claude-sonnet", now=NOW)
        self.assertTrue(pre_ok["allowed"])
        self.assertEqual(pre_ok["decision"], "allow")

        # Check conversational assistant prompt check
        adv = evaluate_assistant_prompt(data, now=NOW)
        self.assertEqual(adv["status"], "warning")
        self.assertIn("gemini-argon", adv["warning"])


if __name__ == "__main__":
    unittest.main()
