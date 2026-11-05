"""Unit tests for PR-to-issue linking and pre-push test runner enforcement (Issue #76)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
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
                           ("GITHUB_REPOSITORY", "GITHUB_WORKSPACE", "GITHUB_EVENT_NAME",
                            "GITHUB_API_URL", "GITHUB_EVENT_PATH", "GH_TOKEN", "GITHUB_TOKEN")}

        class IssueHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in ("/repos/BeOjefe/cgspec", "/repos/mrojefe/cgspec", "/repos/other/cgspec"):
                    repo = self.path.removeprefix("/repos/")
                    payload = json.dumps({"id": 2 if repo == "other/cgspec" else 1,
                                          "full_name": "other/cgspec" if repo == "other/cgspec" else "mrojefe/cgspec"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
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
                           "GITHUB_WORKSPACE": str(self.root), "GITHUB_EVENT_NAME": "test",
                           "GITHUB_API_URL": f"http://127.0.0.1:{self._server.server_port}",
                           "GH_TOKEN": "fixture-token"})
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GITHUB_EVENT_PATH", None)

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

    def test_repository_aliases_work_in_both_directions(self):
        from _lib.issue_linking import existing_issue
        self.assertTrue(existing_issue("BeOjefe/cgspec", "mrojefe/cgspec", 76, "fixture-token"))
        self.assertTrue(existing_issue("mrojefe/cgspec", "BeOjefe/cgspec", 76, "fixture-token"))
        self.assertFalse(existing_issue("BeOjefe/cgspec", "other/cgspec", 76, "fixture-token"))
        os.environ["GITHUB_REPOSITORY"] = "BeOjefe/cgspec"
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        res = self.ci("--pr", "https://github.com/mrojefe/cgspec/issues/76")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_installed_ci_reads_actual_pr_body_without_cli_flag(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        event = self.root / "pr-event.json"
        event.write_text(json.dumps({"pull_request": {"title": "Update logic", "body": "Closes #76"}}))
        os.environ["GITHUB_EVENT_NAME"] = "pull_request"
        os.environ["GITHUB_EVENT_PATH"] = str(event)
        res = self.ci()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("verified PR-to-issue reference", res.stdout)

    def test_installed_ci_rejects_unreadable_pr_event(self):
        self.set_t0()
        (self.root / "src/a.ts").write_text("export const a = 2;\n")
        commit(self.root, "fix: update logic")
        os.environ["GITHUB_EVENT_NAME"] = "pull_request"
        os.environ["GITHUB_EVENT_PATH"] = str(self.root / "missing-event.json")
        res = self.ci()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("GitHub pull_request event payload is unreadable", res.stdout)

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
        event = self.root / "pr-event.json"
        event.write_text(json.dumps({"pull_request": {"title": "Unlinked update", "body": "No issue link"}}))
        os.environ["GITHUB_EVENT_PATH"] = str(event)
        try:
            res = self.ci()
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("candidate PR or commit range has no associated issue reference", res.stdout)
        finally:
            os.environ.pop("GITHUB_EVENT_NAME", None)
            os.environ.pop("GITHUB_WORKSPACE", None)
            os.environ.pop("GITHUB_EVENT_PATH", None)

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

    def set_named_runner(self, test_globs=None):
        from _lib.schema import validate_named
        project = json.loads((self.root / "docs/_cgspec/project.json").read_text())
        project["path_classes"]["tests"] = test_globs or ["tests/**"]
        project.pop("test_command", None)
        project["test_environments"] = [{
            "id": "python", "cwd": ".", "setup": ["python3", "-c", "pass"],
            "readiness": ["python3", "--version"], "inputs": [],
            "network": "online", "timeout_seconds": 30,
        }]
        project["test_runners"] = [{
            "id": "unit", "adapter": "cgspec-json", "command": ["python3", "{test_file}", "{report}"],
            "cwd": ".", "selection": "test_file", "applies_to": ["src/**"],
            "environment": "python", "timeout_seconds": 30, "isolation": "per-run-worktree",
            "report": "json", "artifacts": [],
        }]
        self.assertEqual(validate_named(project, "project"), [])
        write_json(self.root / "docs/_cgspec/project.json", project)

    def write_named_test(self, path: str, passes: bool):
        test = self.root / path
        test.parent.mkdir(parents=True, exist_ok=True)
        test.write_text(
            "import json, pathlib, sys\n"
            f"ok = {passes!r}\n"
            f"pathlib.Path(sys.argv[1]).write_text(json.dumps({{'selector': {path!r}, "
            "'assertions': {'passed': int(ok), 'failed': int(not ok)}, "
            "'infrastructure': {'status': 'ok'}}))\n"
            "raise SystemExit(0 if ok else 1)\n")

    def push_result(self, revision: str, remote: str) -> tuple[int, str]:
        from _lib.local_approval_gate import pre_push
        output = StringIO()
        update = f"refs/heads/codex/test-work {revision} refs/heads/codex/test-work {remote}\n"
        with redirect_stdout(output):
            code = pre_push(self.root, update)
        return code, output.getvalue()

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

    def test_pre_push_finds_monorepo_test_after_docs_only_change(self):
        self.set_project_config(test_command=["python3", "{test_file}"])
        project = self.root / "docs/_cgspec/project.json"
        data = json.loads(project.read_text())
        data["path_classes"]["tests"] = ["packages/*/__tests__/**"]
        write_json(project, data)
        test = self.root / "packages/core/__tests__/passing.py"
        test.parent.mkdir(parents=True)
        test.write_text("raise SystemExit(0)\n")
        previous = commit(self.root, "test: add passing monorepo test")
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- docs only -->\n")
        outgoing = commit(self.root, "docs: update instructions")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 0, output)

    def test_pre_push_finds_named_monorepo_test_after_docs_only_change(self):
        self.set_named_runner(["packages/*/__tests__/**"])
        self.write_named_test("packages/core/__tests__/passing.py", True)
        previous = commit(self.root, "test: add passing named monorepo test")
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- docs only -->\n")
        outgoing = commit(self.root, "docs: update instructions")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 0, output)

    def test_pre_push_honors_explicit_underscored_test_after_change_and_fallback(self):
        from _lib.schema import validate_named
        project = self.root / "docs/_cgspec/project.json"
        data = json.loads(project.read_text())
        data["path_classes"]["tests"] = ["tests/_smoke.py"]
        data["test_command"] = ["python3", "{test_file}"]
        self.assertEqual(validate_named(data, "project"), [])
        write_json(project, data)
        test = self.root / "tests/_smoke.py"
        test.parent.mkdir(exist_ok=True)
        test.write_text("raise SystemExit(0)\n")
        changed = commit(self.root, "test: add explicit smoke test")
        code, output = self.push_result(changed, self.base)
        self.assertEqual(code, 0, output)
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- docs only -->\n")
        outgoing = commit(self.root, "docs: update instructions")
        code, output = self.push_result(outgoing, changed)
        self.assertEqual(code, 0, output)

    def test_pre_push_honors_explicit_underscored_named_runner_test(self):
        self.set_named_runner(["tests/_smoke.py"])
        self.write_named_test("tests/_smoke.py", True)
        outgoing = commit(self.root, "test: add explicit named smoke test")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 0, output)

    def test_pre_push_honors_explicit_named_helper_directory(self):
        self.set_named_runner(["tests/**"])
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        project["test_runners"][0]["test_globs"] = ["tests/helpers/**"]
        write_json(project_file, project)
        self.write_named_test("tests/helpers/setup.py", True)
        outgoing = commit(self.root, "test: add explicitly selected helper suite")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 0, output)

    def test_pre_push_does_not_execute_implicit_fixture_as_test(self):
        self.set_named_runner(["tests/**"])
        self.write_named_test("tests/unit/check.py", True)
        fixture = self.root / "tests/fixtures/data.json"
        fixture.parent.mkdir(parents=True)
        fixture.write_text('{"value": 1}\n')
        outgoing = commit(self.root, "test: add test and fixture")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 0, output)

    def test_pre_push_does_not_infer_helper_as_test_from_broad_glob(self):
        self.set_project_config(test_command=["python3", "{test_file}"])
        test = self.root / "tests/helper.py"
        test.parent.mkdir(exist_ok=True)
        test.write_text("raise SystemExit(0)\n")
        previous = commit(self.root, "test: add helper")
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- docs only -->\n")
        outgoing = commit(self.root, "docs: update instructions")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("no test files were found", output)

    def test_pre_push_touched_helper_cannot_hide_tracked_failing_test(self):
        self.set_project_config(test_command=["python3", "{test_file}"])
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "failing_unit.py").write_text("raise SystemExit(1)\n")
        previous = commit(self.root, "test: add failing unit test")
        (tests / "helper.py").write_text("raise SystemExit(0)\n")
        outgoing = commit(self.root, "test: update helper")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test tests/failing_unit.py failed", output)

    def test_pre_push_touched_helper_directory_cannot_hide_failing_test(self):
        self.set_project_config(test_command=["python3", "{test_file}"])
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "failing_unit.py").write_text("raise SystemExit(1)\n")
        previous = commit(self.root, "test: add failing unit test")
        helper = tests / "helpers/setup.py"
        helper.parent.mkdir()
        helper.write_text("raise SystemExit(0)\n")
        outgoing = commit(self.root, "test: update helper directory")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test tests/failing_unit.py failed", output)

    def test_pre_push_rejects_malformed_outgoing_project_json(self):
        project = self.root / "docs/_cgspec/project.json"
        project.write_text("{invalid json\n")
        outgoing = commit(self.root, "config: broken project policy")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 1)
        self.assertIn("project.json is unreadable or malformed", output)

    def test_pre_push_rejects_non_object_outgoing_project_json(self):
        project = self.root / "docs/_cgspec/project.json"
        project.write_text("[]\n")
        outgoing = commit(self.root, "config: project policy is not an object")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 1)
        self.assertIn("project.json is invalid", output)

    def test_pre_push_rejects_missing_outgoing_project_json(self):
        project = self.root / "docs/_cgspec/project.json"
        project.unlink()
        outgoing = commit(self.root, "config: remove project policy")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 1)
        self.assertIn("outgoing revision has no docs/_cgspec/project.json", output)

    def test_pre_push_rejects_invalid_outgoing_test_configuration(self):
        project = self.root / "docs/_cgspec/project.json"
        data = json.loads(project.read_text())
        data["test_command"] = "python3 -m unittest"
        write_json(project, data)
        outgoing = commit(self.root, "config: invalid test command")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 1)
        self.assertIn("project.json is invalid", output)

    def test_pre_push_ignores_uncommitted_test_repair(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "{test_file}"])
        (self.root / "tests").mkdir(exist_ok=True)
        test = self.root / "tests/failing_unit.py"
        test.write_text("raise SystemExit(1)\n")
        failing = commit(self.root, "test: committed failure")
        test.write_text("raise SystemExit(0)\n")
        ref_update = f"refs/heads/codex/test-work {failing} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)
        self.assertEqual(test.read_text(), "raise SystemExit(0)\n")

    def test_pre_push_uses_outgoing_ref_even_when_head_is_fixed(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "{test_file}"])
        (self.root / "tests").mkdir(exist_ok=True)
        test = self.root / "tests/failing_unit.py"
        test.write_text("raise SystemExit(1)\n")
        failing = commit(self.root, "test: committed failure")
        test.write_text("raise SystemExit(0)\n")
        commit(self.root, "test: fixed on HEAD only")
        ref_update = f"refs/heads/codex/older {failing} refs/heads/codex/older {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

    def test_pre_push_ignores_uncommitted_config_repair(self):
        from _lib.local_approval_gate import pre_push
        self.set_project_config(test_command=["python3", "-c", "raise SystemExit(1)"])
        failing = commit(self.root, "config: committed failing test command")
        self.set_project_config(test_command=["python3", "-c", "raise SystemExit(0)"])
        ref_update = f"refs/heads/codex/test-work {failing} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

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
        self.set_project_config(test_command=["python3", "{test_file}"])
        current_head = commit(self.root, "docs: outgoing work")
        code, output = self.push_result(current_head, self.base)
        self.assertEqual(code, 1)
        self.assertIn("no test files were found", output)

    def test_pre_push_blocks_when_test_file_runner_selects_none(self):
        from _lib.local_approval_gate import pre_push
        self.set_named_runner()
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

    def test_pre_push_runner_does_not_select_unrelated_test_files(self):
        from _lib.local_approval_gate import pre_push
        self.set_named_runner(["tests/unit/**"])
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/unrelated.py").write_text("pass\n")
        current_head = commit(self.root, "test: unrelated test")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

    def test_pre_push_executes_test_runners_with_test_file_substitution(self):
        from _lib.local_approval_gate import pre_push
        self.set_named_runner()
        self.write_named_test("tests/failing_runner.py", False)
        current_head = commit(self.root, "test: add failing runner test")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

        self.write_named_test("tests/failing_runner.py", True)
        fixed_head = commit(self.root, "test: fix runner test")
        ref_update = f"refs/heads/codex/test-work {fixed_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 0)

    def test_pre_push_executes_named_runners_alongside_legacy_command(self):
        self.set_named_runner()
        self.write_named_test("tests/failing_runner.py", False)
        self.set_project_config(test_command=["python3", "-c", "raise SystemExit(0)"])
        outgoing = commit(self.root, "test: add failing named test during migration")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 1)
        self.assertIn("test runner unit on tests/failing_runner.py failed", output)

    def test_pre_push_routes_test_files_to_declared_named_runner(self):
        self.set_named_runner()
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        unit = project["test_runners"][0]
        unit["test_globs"] = ["tests/unit/**"]
        unit["command"] = ["python3", "{test_file}", "{report}", "unit"]
        browser = dict(unit, id="browser", test_globs=["tests/browser/**"],
                       command=["python3", "{test_file}", "{report}", "browser"])
        project["test_runners"] = [unit, browser]
        write_json(project_file, project)
        for path, expected in (("tests/unit/check.py", "unit"),
                               ("tests/browser/check.py", "browser")):
            test = self.root / path
            test.parent.mkdir(parents=True, exist_ok=True)
            test.write_text(
                "import json, pathlib, sys\n"
                f"ok = sys.argv[2] == {expected!r}\n"
                f"pathlib.Path(sys.argv[1]).write_text(json.dumps({{'selector': {path!r}, "
                "'assertions': {'passed': int(ok), 'failed': int(not ok)}, "
                "'infrastructure': {'status': 'ok'}}))\n"
                "raise SystemExit(0 if ok else 1)\n")
        outgoing = commit(self.root, "test: add runner-specific tests")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 0, output)

    def test_pre_push_source_change_runs_every_affected_runner_suite(self):
        self.set_named_runner()
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        unit = project["test_runners"][0]
        unit["test_globs"] = ["tests/unit/**"]
        browser = dict(unit, id="browser", test_globs=["tests/browser/**"])
        project["test_runners"] = [unit, browser]
        write_json(project_file, project)
        self.write_named_test("tests/unit/check.py", True)
        self.write_named_test("tests/browser/check.py", False)
        previous = commit(self.root, "test: establish runner suites")
        unit_test = self.root / "tests/unit/check.py"
        unit_test.write_text(unit_test.read_text() + "\n")
        source = self.root / "src/a.ts"
        source.parent.mkdir(exist_ok=True)
        source.write_text("export const a = 2;\n")
        outgoing = commit(self.root, "fix: change source with unit coverage")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test runner browser on tests/browser/check.py failed", output)

    def test_pre_push_source_change_rejects_empty_affected_runner_suite(self):
        self.set_named_runner()
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        unit = project["test_runners"][0]
        unit["test_globs"] = ["tests/unit/**"]
        browser = dict(unit, id="browser", test_globs=["tests/browser/**"], applies_to=["web/**"])
        project["test_runners"] = [unit, browser]
        write_json(project_file, project)
        self.write_named_test("tests/browser/check.py", True)
        previous = commit(self.root, "test: establish unrelated suite")
        source = self.root / "src/a.ts"
        source.parent.mkdir(exist_ok=True)
        source.write_text("export const a = 2;\n")
        outgoing = commit(self.root, "fix: change source without runner suite")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test runner unit selects no test files", output)

    def test_pre_push_runner_input_change_runs_every_affected_suite(self):
        self.set_named_runner()
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        project["test_environments"][0]["inputs"] = ["runner.lock"]
        unit = project["test_runners"][0]
        unit["test_globs"] = ["tests/unit/**"]
        browser = dict(unit, id="browser", test_globs=["tests/browser/**"])
        project["test_runners"] = [unit, browser]
        write_json(project_file, project)
        (self.root / "runner.lock").write_text("first\n")
        self.write_named_test("tests/unit/check.py", True)
        self.write_named_test("tests/browser/check.py", False)
        previous = commit(self.root, "test: establish input-bound suites")
        (self.root / "runner.lock").write_text("second\n")
        unit_test = self.root / "tests/unit/check.py"
        unit_test.write_text(unit_test.read_text() + "\n")
        outgoing = commit(self.root, "build: update shared runner input")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test runner browser on tests/browser/check.py failed", output)

    def test_pre_push_unrouted_helper_change_runs_all_named_suites(self):
        self.set_named_runner()
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        unit = project["test_runners"][0]
        unit["test_globs"] = ["tests/unit/**"]
        browser = dict(unit, id="browser", test_globs=["tests/browser/**"])
        project["test_runners"] = [unit, browser]
        write_json(project_file, project)
        self.write_named_test("tests/unit/check.py", True)
        self.write_named_test("tests/browser/check.py", False)
        helper = self.root / "tests/helpers/shared.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("VALUE = 1\n")
        previous = commit(self.root, "test: establish suites and shared helper")
        helper.write_text("VALUE = 2\n")
        unit_test = self.root / "tests/unit/check.py"
        unit_test.write_text(unit_test.read_text() + "\n")
        outgoing = commit(self.root, "test: update shared helper with unit test")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test runner browser on tests/browser/check.py failed", output)

    def test_pre_push_broad_test_glob_does_not_route_implicit_helper(self):
        self.set_named_runner(["tests/**", "e2e/**"])
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        unit = project["test_runners"][0]
        unit["test_globs"] = ["tests/**"]
        browser = dict(unit, id="browser", test_globs=["e2e/**"])
        project["test_runners"] = [unit, browser]
        write_json(project_file, project)
        self.write_named_test("tests/unit/check.py", True)
        self.write_named_test("e2e/browser/check.py", False)
        helper = self.root / "tests/helpers/shared.py"
        helper.parent.mkdir(parents=True)
        helper.write_text("VALUE = 1\n")
        previous = commit(self.root, "test: establish broad suites and helper")
        helper.write_text("VALUE = 2\n")
        unit_test = self.root / "tests/unit/check.py"
        unit_test.write_text(unit_test.read_text() + "\n")
        outgoing = commit(self.root, "test: update implicit helper with unit test")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test runner browser on e2e/browser/check.py failed", output)

    def test_pre_push_rejects_affected_runner_whose_last_test_was_deleted(self):
        self.set_named_runner()
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        unit = project["test_runners"][0]
        unit["test_globs"] = ["tests/unit/**"]
        browser = dict(unit, id="browser", test_globs=["tests/browser/**"])
        project["test_runners"] = [unit, browser]
        write_json(project_file, project)
        self.write_named_test("tests/unit/check.py", True)
        self.write_named_test("tests/browser/check.py", True)
        previous = commit(self.root, "test: establish both runner suites")
        (self.root / "tests/browser/check.py").unlink()
        outgoing = commit(self.root, "test: remove final browser test")
        code, output = self.push_result(outgoing, previous)
        self.assertEqual(code, 1)
        self.assertIn("test runner browser selects no test files", output)

    def test_pre_push_rejects_ambiguous_multiple_runner_selection(self):
        self.set_named_runner()
        project_file = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_file.read_text())
        project["test_runners"].append(dict(project["test_runners"][0], id="browser"))
        write_json(project_file, project)
        self.write_named_test("tests/check.py", True)
        outgoing = commit(self.root, "test: add ambiguously routed test")
        code, output = self.push_result(outgoing, self.base)
        self.assertEqual(code, 1)
        self.assertIn("multiple test_runners must each declare non-empty test_globs", output)

    def test_pre_push_rejects_runner_without_test_file_selection(self):
        from _lib.local_approval_gate import pre_push
        self.set_named_runner()
        project = json.loads((self.root / "docs/_cgspec/project.json").read_text())
        project["test_runners"][0]["command"] = ["python3", "-c", "pass"]
        write_json(self.root / "docs/_cgspec/project.json", project)
        agents_file = self.root / "AGENTS.md"
        agents_file.write_text(agents_file.read_text() + "\n<!-- push work -->\n")
        current_head = commit(self.root, "docs: outgoing work")
        ref_update = f"refs/heads/codex/test-work {current_head} refs/heads/codex/test-work {self.base}\n"
        self.assertEqual(pre_push(self.root, ref_update), 1)

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
