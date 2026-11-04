"""Unit tests for PR-to-issue linking and pre-push test runner enforcement (Issue #76)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.helpers import TempDir, git, git_project, hook, make_slice, run_script, set_test_command, write_json


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").stdout.strip()


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "-m", message)
    return head(root)


class IssueLinkingGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        git_project(self.root, profile="cli")
        self.base = head(self.root)
        self._saved_env = {key: os.environ.get(key) for key in
                           ("GITHUB_REPOSITORY", "GITHUB_API_URL", "GH_TOKEN", "GITHUB_TOKEN")}

        class IssueHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                number = self.path.rsplit("/", 1)[-1]
                repo = self.path.removeprefix("/repos/").rsplit("/issues/", 1)[0]
                if repo in ("BeOjefe/cgspec", "mrojefe/cgspec", "other/cgspec") and number in ("76", "77"):
                    canonical = "other/cgspec" if repo == "other/cgspec" else "mrojefe/cgspec"
                    body = {"number": int(number),
                            "repository_url": f"http://127.0.0.1:{self.server.server_port}/repos/{canonical}"}
                    if number == "77":
                        body["pull_request"] = {"url": "https://example.invalid/pr/77"}
                    payload = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(int(number) if number in ("403", "429") else 404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), IssueHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        os.environ.update({"GITHUB_REPOSITORY": "mrojefe/cgspec",
                           "GITHUB_API_URL": f"http://127.0.0.1:{self._server.server_port}",
                           "GH_TOKEN": "fixture-token"})
        os.environ.pop("GITHUB_TOKEN", None)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.__exit__(None, None, None)

    def ci(self, *extra: str):
        return hook(self.root, "ci", None, "--tool", "git", "--base", self.base, *extra)

    def set_t0(self, file="src/a.ts"):
        res = run_script("cgspec_hook.py", "tier", "set", "--tier", "T0", "--reason", "code update",
                         "--files", file, "--verify", "unit test", "--root", str(self.root))
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_unlinked_pr_fails_governance_gate(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic without issue reference")
        res = self.ci("--pr", "")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("candidate PR or commit range has no associated issue reference", res.stdout)

    def test_pr_metadata_with_issue_number_passes_governance_gate(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        res = self.ci("--pr", "fixes #76")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("verified PR-to-issue reference", res.stdout)

    def test_pr_metadata_with_issue_url_passes_governance_gate(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        res = self.ci("--pr", "https://github.com/BeOjefe/cgspec/issues/76")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("verified PR-to-issue reference", res.stdout)

    def test_commit_message_with_closes_issue_passes_governance_gate(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic\n\nCloses #76")
        res = self.ci("--pr", "")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("verified PR-to-issue reference", res.stdout)

    def test_commit_message_with_inline_issue_passes_governance_gate(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic (#76)")
        res = self.ci("--pr", "")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("verified PR-to-issue reference", res.stdout)

    def test_branch_level_issue_association_passes_governance_gate(self):
        git(self.root, "checkout", "-q", "-b", "issue-76-governance")
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic on branch")
        res = self.ci("--pr", "")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("verified PR-to-issue reference", res.stdout)

    def test_project_json_require_issue_linking_enforces_without_pr_flag(self):
        pj = self.root / "docs/_cgspec/project.json"
        os.chmod(pj, 0o644)
        data = json.loads(pj.read_text())
        data["require_issue_linking"] = True
        pj.write_text(json.dumps(data, indent=2) + "\n")
        commit(self.root, "config: enable require_issue_linking")
        self.base = head(self.root)

        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: unlinked change")
        res = self.ci()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("candidate PR or commit range has no associated issue reference", res.stdout)

        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 3;\n")
        commit(self.root, "fix: linked change (fixes #76)")
        res2 = self.ci()
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)

    def test_require_issue_cli_flag_enforces_linking(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        res = self.ci("--require-issue")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("candidate PR or commit range has no associated issue reference", res.stdout)

    def test_pr_metadata_with_arbitrary_numbers_fails_without_issue_link(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        res = self.ci("--pr", "Refactor phase 2 of login module without issue reference")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("candidate PR or commit range has no associated issue reference", res.stdout)

    def test_pr_metadata_with_numeric_id_only_fails(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        res = self.ci("--pr", "76")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("no associated issue reference", res.stdout)

    def test_nonexistent_issue_and_pr_number_fail(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        for reference in ("fixes #999999", "fixes #77", "PR #76"):
            with self.subTest(reference=reference):
                res = self.ci("--pr", reference)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)

    def test_wrong_repository_issue_url_fails(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        res = self.ci("--pr", "https://github.com/other/cgspec/issues/76")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("no referenced existing issue in mrojefe/cgspec", res.stdout)

    def test_missing_token_and_api_errors_fail_closed(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        os.environ.pop("GH_TOKEN")
        res = self.ci("--pr", "fixes #76")
        self.assertIn("GH_TOKEN or GITHUB_TOKEN", res.stdout)
        os.environ["GH_TOKEN"] = "fixture-token"
        for number in (403, 429):
            with self.subTest(status=number):
                res = self.ci("--pr", f"fixes #{number}")
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertIn(f"HTTP {number}", res.stdout)
        os.environ["GITHUB_API_URL"] = "http://127.0.0.1:1"
        res = self.ci("--pr", "fixes #76")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("lookup unavailable", res.stdout)

    def test_branch_with_step_or_patch_number_fails_linking(self):
        git(self.root, "checkout", "-q", "-b", "refactor-step-1")
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic on step branch")
        res = self.ci("--pr", "")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("candidate PR or commit range has no associated issue reference", res.stdout)

    def test_branch_with_feat_slash_issue_number_passes(self):
        git(self.root, "checkout", "-q", "-b", "feat/76-login")
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic on feat issue branch")
        res = self.ci("--pr", "")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("verified PR-to-issue reference", res.stdout)

    def test_ci_pr_environment_variable_enforces_linking(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: unlinked pr commit")
        os.environ["GITHUB_EVENT_NAME"] = "pull_request"
        os.environ["GITHUB_WORKSPACE"] = str(self.root)
        try:
            res = self.ci()
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("candidate PR or commit range has no associated issue reference", res.stdout)
        finally:
            os.environ.pop("GITHUB_EVENT_NAME", None)
            os.environ.pop("GITHUB_WORKSPACE", None)

    def test_unconfigured_without_pr_flag_preserves_legacy_behavior(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: general update")
        res = self.ci()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("candidate PR or commit range has no associated issue reference", res.stdout)


class PrePushTestExecutionGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        git_project(self.root)
        self.base = head(self.root)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def set_project_config(self, **kwargs):
        pj = self.root / "docs/_cgspec/project.json"
        os.chmod(pj, 0o644)
        data = json.loads(pj.read_text())
        data.update(kwargs)
        pj.write_text(json.dumps(data, indent=2) + "\n")

    def test_pre_push_blocks_when_configured_test_command_fails(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "-c", "import sys; print('TEST_FAILURE_DETECTED'); sys.exit(1)"])
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- push work -->\n")
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        code = pre_push(self.root, ref_update)
        self.assertEqual(code, 1)

    def test_pre_push_passes_when_configured_test_command_succeeds(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "-c", "import sys; sys.exit(0)"])
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- push work -->\n")
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        code = pre_push(self.root, ref_update)
        self.assertEqual(code, 0)

    def test_pre_push_executes_test_file_pattern_on_changed_tests(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "{test_file}"])
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/failing_unit.py").write_text("import sys\nsys.exit(1)\n")
        current_head = commit(self.root, "test: add failing unit test")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

        (self.root / "tests/failing_unit.py").write_text("import sys\nsys.exit(0)\n")
        fixed_head = commit(self.root, "test: fix unit test")
        ref_update = f"refs/heads/codex/test-work {fixed_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 0)

    def test_pre_push_ref_deletion_skips_tests(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "-c", "import sys; sys.exit(1)"])
        zero = "0" * 40
        ref_update = f"refs/heads/branch-to-delete {zero} refs/heads/branch-to-delete {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 0)

    def test_pre_push_skip_env_var_cannot_bypass_tests(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "-c", "import sys; sys.exit(1)"])
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- push work -->\n")
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        os.environ["CGSPEC_SKIP_PRE_PUSH_TESTS"] = "1"
        try:
            self.assertEqual(pre_push(self.root, ref_update), 1)
        finally:
            os.environ.pop("CGSPEC_SKIP_PRE_PUSH_TESTS", None)

    def test_pre_push_blocks_when_test_file_command_selects_none(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "{test_file}"])
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

    def test_pre_push_blocks_when_test_file_runner_selects_none(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=None, test_runners=[{
            "id": "unit", "command": ["python3", "{test_file}"],
            "cwd": ".", "timeout_seconds": 30, "applies_to": ["tests/**"],
        }])
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

    def test_pre_push_runner_does_not_select_unrelated_test_files(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=None, test_runners=[{
            "id": "unit", "command": ["python3", "{test_file}"],
            "cwd": ".", "timeout_seconds": 30, "applies_to": ["tests/unit/**"],
        }])
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/unrelated.py").write_text("pass\n")
        current_head = commit(self.root, "test: unrelated test")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

    def test_pre_push_executes_test_runners_with_test_file_substitution(self):
        from _lib.local_approval_gate import pre_push
        runner = {
            "id": "unit",
            "command": ["python3", "{test_file}"],
            "cwd": ".",
            "timeout_seconds": 30,
            "applies_to": ["tests/**"],
        }
        self.set_project_config(test_command=None, test_runners=[runner])
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/failing_runner.py").write_text("import sys\nsys.exit(1)\n")
        current_head = commit(self.root, "test: add failing runner test")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

        (self.root / "tests/failing_runner.py").write_text("import sys\nsys.exit(0)\n")
        fixed_head = commit(self.root, "test: fix runner test")
        ref_update = f"refs/heads/codex/test-work {fixed_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 0)

    def test_pre_push_executes_test_runners_without_test_file(self):
        from _lib.local_approval_gate import pre_push
        runner = {
            "id": "suite",
            "command": ["python3", "-c", "import sys; sys.exit(0)"],
            "cwd": ".",
            "timeout_seconds": 30,
        }
        self.set_project_config(test_command=None, test_runners=[runner])
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- push work -->\n")
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 0)

    def test_git_pre_push_event_alias(self):
        self.set_project_config(test_command=["python3", "-c", "import sys; sys.exit(0)"])
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- change -->\n")
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        res = run_script("cgspec_hook.py", "git-pre-push", "--root", str(self.root), "--tool", "git", stdin=ref_update)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("CGSpec local approval check passed", res.stdout)


if __name__ == "__main__":
    unittest.main()
