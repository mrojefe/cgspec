"""Named runner contracts must bind proof without replacing legacy records."""

from __future__ import annotations

import copy
import json
import shutil
import unittest

from tests.unit.test_schemas import VALID
from _lib.schema import validate_named
from _lib import runner_contracts, test_evolution
from _lib.common import CgspecError
from _lib.gitview import View
from _lib.runner_trust import check as runner_change
from tests.helpers import REPO, TempDir, approve, git, git_project, write_json


SEMANTICS = {"adapter": "command-json-v1", "fingerprint": "b" * 64, "dependencies": []}

def runner_project() -> dict:
    project = copy.deepcopy(VALID["project"])
    project["test_environments"] = [{
        "id": "node-tests", "cwd": ".", "setup": ["npm", "ci"],
        "readiness": ["node", "--version"], "inputs": [], "network": "online", "timeout_seconds": 120,
    }]
    project["test_runners"] = [
        {"id": "unit", "adapter": "vitest-json", "command": ["npm", "exec", "--", "vitest", "run", "{test_file}", "--reporter=json", "--outputFile={report}"],
         "cwd": ".", "selection": "test_file", "test_globs": ["tests/**/*.test.js"],
         "applies_to": ["src/**"], "environment": "node-tests", "timeout_seconds": 120,
         "isolation": "per-run-worktree", "report": "json", "artifacts": [], "semantics": {"adapter": "command-json-v1", "command": ["node", "tools/semantic-adapter.js", "{test_file}", "{test_id}"], "inputs": ["tools/semantic-adapter.js"]}},
        {"id": "browser", "adapter": "playwright-json", "command": ["npm", "exec", "--", "playwright", "test", "{test_file}", "--reporter=json"],
         "cwd": ".", "selection": "test_file", "test_globs": ["tests/**/*.spec.js"],
         "applies_to": ["src/**"], "environment": "node-tests", "timeout_seconds": 180,
         "isolation": "per-run-worktree", "report": "stdout-json", "artifacts": ["test-results/**"], "semantics": {"adapter": "command-json-v1", "command": ["node", "tools/semantic-adapter.js", "{test_file}", "{test_id}"], "inputs": ["tools/semantic-adapter.js"]}},
    ]
    project["test_runners_approval"] = "trailer"
    return project


class NamedRunnerSchemaTests(unittest.TestCase):
    def test_multiple_runners_and_shared_environment_are_valid(self):
        self.assertEqual(validate_named(runner_project(), "project"), [])

    def test_runner_id_and_environment_reference_are_bounded(self):
        project = runner_project()
        project["test_runners"][0]["id"] = "../escape"
        self.assertTrue(validate_named(project, "project"))
        project = runner_project()
        project["test_runners"][0]["environment"] = None
        self.assertTrue(validate_named(project, "project"))

    def test_new_proof_identifies_runner_and_definition_hash(self):
        record = copy.deepcopy(VALID["test-pair"])
        record.update(runner_id="browser", runner_hash="a" * 64, selector="tests/login.spec.ts")
        self.assertEqual(validate_named(record, "test-pair"), [])

    def test_hash_binds_runner_and_shared_environment(self):
        project = runner_project()
        original = runner_contracts.resolve(project, "browser", lambda _: b"adapter")[2]
        changed = copy.deepcopy(project)
        changed["test_environments"][0]["setup"] = ["npm", "ci", "--ignore-scripts"]
        self.assertNotEqual(original, runner_contracts.resolve(changed, "browser")[2])
        self.assertNotEqual(original, runner_contracts.resolve(changed, "browser", lambda _: b"adapter")[2])
        changed = copy.deepcopy(project)
        changed["test_runners"][1]["command"].append("--grep=only-one")
        self.assertNotEqual(original, runner_contracts.resolve(changed, "browser", lambda _: b"adapter")[2])
        changed = copy.deepcopy(project)
        changed["test_runners"][1]["test_globs"] = ["tests/e2e/**"]
        self.assertNotEqual(original, runner_contracts.resolve(changed, "browser", lambda _: b"adapter")[2])

    def test_hash_binds_declared_input_bytes(self):
        project = runner_project()
        project["test_environments"][0]["inputs"] = ["package-lock.json"]
        first = runner_contracts.resolve(project, "unit", lambda _: b"first")[2]
        second = runner_contracts.resolve(project, "unit", lambda _: b"second")[2]
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(CgspecError, "input package-lock.json is missing"):
            runner_contracts.resolve(project, "unit", lambda _: None)

    def test_duplicate_id_unknown_environment_and_selection_are_rejected(self):
        project = runner_project()
        project["test_runners"][1]["id"] = "unit"
        self.assertTrue(runner_contracts.errors(project))
        project = runner_project()
        project["test_runners"][1]["environment"] = "missing"
        self.assertTrue(runner_contracts.errors(project))
        project = runner_project()
        project["test_runners"][1]["command"].remove("{test_file}")
        self.assertTrue(runner_contracts.errors(project))

    def test_offline_cache_requires_a_pinned_lockfile_input(self):
        project = runner_project()
        project["test_environments"][0]["network"] = "offline-cache"
        self.assertTrue(runner_contracts.errors(project))
        project["test_environments"][0]["inputs"] = ["package-lock.json"]
        self.assertEqual(runner_contracts.errors(project), [])

    def test_unlocked_dependency_resolution_is_rejected(self):
        for setup in (["npm", "install"], ["npm", "install", "lodash"], ["npm", "i"], ["npm", "add", "lodash"],
                      ["yarn", "upgrade"], ["pip3", "install", "flask"], ["poetry", "add", "requests"]):
            project = runner_project()
            project["test_environments"][0]["setup"] = setup
            self.assertTrue(runner_contracts.errors(project), setup)
        project = runner_project()
        project["test_environments"][0]["setup"] = ["npm", "ci"]
        self.assertEqual(runner_contracts.errors(project), [])
        project = runner_project()
        project["test_environments"][0]["cleanup"] = ["npm", "i", "lodash"]
        self.assertTrue(runner_contracts.errors(project))

    def test_network_defaults_to_online_for_existing_environments(self):
        project = runner_project()
        del project["test_environments"][0]["network"]
        self.assertEqual(validate_named(project, "project"), [])
        self.assertEqual(runner_contracts.errors(project), [])

    def test_new_proof_cannot_substitute_runner_command_or_selector(self):
        project = runner_project()
        runner, _, digest = runner_contracts.resolve(project, "unit", lambda _: b"adapter")
        record = copy.deepcopy(VALID["test-pair"])
        record.update(runner_id="unit", runner_hash=digest, selector=record["test_file"],
                      command=runner_contracts.selected_command(runner, record["test_file"]), test_semantics=SEMANTICS)
        self.assertEqual(runner_contracts.pair_errors(project, record, lambda _: b"adapter"), [])
        changed = copy.deepcopy(record)
        changed["command"] = ["true", changed["test_file"]]
        self.assertTrue(runner_contracts.pair_errors(project, changed))
        changed = copy.deepcopy(record)
        changed["selector"] = "tests/a-different.test.ts"
        self.assertTrue(runner_contracts.pair_errors(project, changed))
        changed = copy.deepcopy(record)
        changed["runner_hash"] = "0" * 64
        self.assertTrue(runner_contracts.pair_errors(project, changed))


class NamedRunnerApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.root = self.tmp.__enter__()
        git_project(self.root)
        self.path = self.root / "docs/_cgspec/project.json"

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def configure(self, ref: str):
        data = json.loads(self.path.read_text())
        example = runner_project()
        data.update({key: example[key] for key in ("test_runners", "test_environments")})
        data["test_runners_approval"] = ref
        write_json(self.path, data)
        git(self.root, "add", "docs")
        return runner_change(View.staged(self.root))

    def test_new_runner_contract_needs_targeted_fresh_approval(self):
        self.assertTrue(self.configure("missing"))
        ref = approve(self.root, "approve test runners")
        self.assertEqual(self.configure(ref), [])

    def test_changing_setup_needs_new_approval(self):
        ref = approve(self.root, "approve test runners")
        self.assertEqual(self.configure(ref), [])
        git(self.root, "commit", "-q", "-m", "approved runners")
        data = json.loads(self.path.read_text())
        data["test_environments"][0]["setup"] = ["npm", "ci", "--ignore-scripts"]
        write_json(self.path, data)
        git(self.root, "add", "docs")
        self.assertTrue(runner_change(View.staged(self.root)))


class AssertionAdapterTests(unittest.TestCase):
    def test_vitest_failure_is_assertion_not_infrastructure(self):
        from _lib.runner_execution import classify
        report = {"numTotalTests": 1, "numFailedTests": 1,
                  "testResults": [{"assertionResults": [{"status": "failed", "failureMessages": [
                      "AssertionError: expected 0 to be 1"]}]}]}
        self.assertEqual(classify("vitest-json", report, 1), "ASSERTION_FAILED")
        self.assertEqual(classify("vitest-json", report, 0), "INFRASTRUCTURE_ERROR")
        self.assertEqual(classify("vitest-json", {"testResults": []}, 1), "INFRASTRUCTURE_ERROR")
        report["testResults"][0]["assertionResults"][0]["failureMessages"] = ["TypeError: undefined is not a function"]
        self.assertEqual(classify("vitest-json", report, 1), "INFRASTRUCTURE_ERROR")

    def test_vitest_mixed_assertion_and_unhandled_error_is_infrastructure(self):
        from _lib.runner_execution import classify
        report = {"numTotalTests": 1, "numFailedTests": 1,
                  "testResults": [{"assertionResults": [{"status": "failed", "failureMessages": [
                      "AssertionError: expected 0 to be 1"]}]}],
                  "unhandledErrors": [{"message": "worker crashed"}]}
        self.assertEqual(classify("vitest-json", report, 1), "INFRASTRUCTURE_ERROR")

    def test_playwright_failure_is_assertion_not_infrastructure(self):
        from _lib.runner_execution import classify
        report = {"errors": [], "suites": [{"specs": [{"tests": [{"results": [
            {"status": "failed", "error": {"message": "AssertionError: expected true"}}]}]}]}]}
        self.assertEqual(classify("playwright-json", report, 1), "ASSERTION_FAILED")
        passing = {"suites": [{"specs": [{"tests": [{"results": [{"status": "passed"}]}]}]}]}
        self.assertEqual(classify("playwright-json", passing, 0), "PASS")
        self.assertEqual(classify("playwright-json", {"suites": []}, 1), "INFRASTRUCTURE_ERROR")

    def test_generic_adapter_requires_explicit_assertions_and_healthy_infrastructure(self):
        from _lib.runner_execution import classify
        report = {"assertions": {"passed": 0, "failed": 1}, "infrastructure": {"status": "ok"}}
        self.assertEqual(classify("cgspec-json", report, 1), "ASSERTION_FAILED")
        report["infrastructure"]["status"] = "error"
        self.assertEqual(classify("cgspec-json", report, 1), "INFRASTRUCTURE_ERROR")

    def test_browser_report_uses_selected_file_relative_to_test_dir(self):
        from _lib.runner_execution import classify
        report = {"suites": [{"file": "browser.spec.js", "specs": [{"tests": [
            {"results": [{"status": "failed", "error": {"message": "Error: expect(locator).toBeVisible() failed"}}]}]}]}],
                  "errors": [], "stats": {"unexpected": 1}}
        self.assertEqual(classify("playwright-json", report, 1, "tests/browser.spec.js"), "ASSERTION_FAILED")
        report["suites"][0]["file"] = "other.spec.js"
        self.assertEqual(classify("playwright-json", report, 1, "tests/browser.spec.js"), "INFRASTRUCTURE_ERROR")

    def test_browser_runtime_error_is_not_a_failing_assertion(self):
        from _lib.runner_execution import classify
        report = {"suites": [{"file": "browser.spec.js", "specs": [{"tests": [
            {"results": [{"status": "failed", "error": {"message": "TypeError: page is undefined"}}]}]}]}],
                  "errors": [], "stats": {"unexpected": 1}}
        self.assertEqual(classify("playwright-json", report, 1, "tests/browser.spec.js"), "INFRASTRUCTURE_ERROR")
        report["suites"][0]["specs"][0]["tests"][0]["results"][0]["error"]["message"] = "Error: expect(1).toBe(2)"
        report["errors"] = [{"message": "configuration import failed"}]
        self.assertEqual(classify("playwright-json", report, 1, "tests/browser.spec.js"), "INFRASTRUCTURE_ERROR")


class NamedRunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.root = self.tmp.__enter__()
        git_project(self.root)
        project_path = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_path.read_text())
        project["test_environments"] = [{"id": "python", "cwd": ".", "setup": ["python3", "-c", "pass"],
                                         "readiness": ["python3", "--version"], "inputs": ["tools/semantic_adapter.py"], "network": "online", "timeout_seconds": 10}]
        project["test_runners"] = [{"id": "unit", "adapter": "cgspec-json",
                                    "command": ["python3", "{test_file}", "{report}"], "cwd": ".",
                                    "selection": "test_file", "applies_to": ["src/**"], "environment": "python", "timeout_seconds": 10,
                                    "isolation": "per-run-worktree", "report": "json", "artifacts": [], "semantics": {"adapter": "command-json-v1", "command": ["python3", "tools/semantic_adapter.py", "{test_file}", "{test_id}"], "inputs": ["tools/semantic_adapter.py"]}}]
        write_json(project_path, project)
        tools = self.root / "tools"
        tools.mkdir()
        shutil.copy2(REPO / "tests/fixtures/semantic-python-adapter.py", tools / "semantic_adapter.py")
        tests = self.root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "check.py").write_text(
            "import json, pathlib, sys\n"
            "def test_check():\n    return pathlib.Path('src/login/fixed.txt').exists()\n"
            "ok = test_check()\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'selector': 'tests/check.py', 'assertions': {'passed': int(ok), 'failed': int(not ok)}, 'infrastructure': {'status': 'ok'}}))\n"
            "raise SystemExit(0 if ok else 1)\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "named runner and failing test")
        self.failed_at = git(self.root, "rev-parse", "HEAD").stdout.strip()
        (self.root / "src/login/fixed.txt").write_text("fixed\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "fix")
        runner, environment, digest = runner_contracts.resolve(project, "unit", lambda rel: (self.root / rel).read_bytes())
        identity, problem = test_evolution.capture_at(self.root, self.failed_at, runner, environment, "tests/check.py", "tests/check.py::test_check")
        self.assertIsNone(problem)
        self.record = {"_file": "docs/_cgspec/06-evidence/tests/check.json", "test_id": "tests/check.py::test_check",
                       "test_file": "tests/check.py", "failed_at": self.failed_at, "runner_id": "unit",
                       "runner_hash": digest, "selector": "tests/check.py",
                       "command": runner_contracts.selected_command(runner, "tests/check.py"),
                       "covers": ["src/login/fixed.txt"], "slice": "SLICE-001", "test_semantics": identity}

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_real_red_green_uses_assertion_report_in_isolated_revisions(self):
        from _lib.ci_gate import prove_failed
        before = git(self.root, "status", "--porcelain").stdout
        self.assertIsNone(prove_failed(self.root, self.record))
        self.assertEqual(git(self.root, "status", "--porcelain").stdout, before)

    def test_runner_hash_substitution_does_not_prove_red_green(self):
        from _lib.ci_gate import prove_failed
        self.record["runner_hash"] = "0" * 64
        self.assertIn("configuration hash", prove_failed(self.root, self.record))

    def test_setup_failure_cannot_be_mistaken_for_red_assertion(self):
        from _lib.ci_gate import prove_failed
        project_path = self.root / "docs/_cgspec/project.json"
        project = json.loads(project_path.read_text())
        project["test_environments"][0]["setup"] = ["python3", "-c", "raise SystemExit(1)"]
        write_json(project_path, project)
        git(self.root, "add", "docs")
        git(self.root, "commit", "-q", "-m", "broken setup")
        from _lib.runner_execution import run_at
        runner, environment, _ = runner_contracts.resolve(project, "unit", lambda rel: (self.root / rel).read_bytes())
        runner, environment, _ = runner_contracts.resolve(project, "unit", lambda rel: (self.root / rel).read_bytes())
        self.assertEqual(run_at(self.root, "HEAD", runner, environment, "tests/check.py"), "BLOCKED")

    def test_parallel_runs_have_separate_worktrees_and_reports(self):
        from concurrent.futures import ThreadPoolExecutor
        from _lib.runner_execution import run_at
        project = json.loads((self.root / "docs/_cgspec/project.json").read_text())
        runner, environment, _ = runner_contracts.resolve(project, "unit", lambda rel: (self.root / rel).read_bytes())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run_at(self.root, "HEAD", runner, environment, "tests/check.py"), range(2)))
        self.assertEqual(results, ["PASS", "PASS"])
        self.assertEqual(len(git(self.root, "worktree", "list").stdout.splitlines()), 1)

    def test_candidate_gate_rejects_new_unbound_runner_proof(self):
        from _lib.commit_gate import _test_pairs
        project = json.loads((self.root / "docs/_cgspec/project.json").read_text())
        path = self.root / self.record["_file"]
        record = {key: value for key, value in self.record.items() if key != "_file"}
        record.update(schema=1, recorded="2026-10-28T00:00:00Z")
        write_json(path, record)
        git(self.root, "add", "docs")
        tier = {"tier": "T1", "slice": "SLICE-001"}
        classes = project["path_classes"]
        self.assertEqual(_test_pairs(View.staged(self.root), [], tier, classes, project), [])
        record["runner_hash"] = "0" * 64
        write_json(path, record)
        git(self.root, "add", "docs")
        self.assertTrue(any("configuration hash" in reason for reason in
                            _test_pairs(View.staged(self.root), [], tier, classes, project)))

    def test_post_migration_legacy_pair_cannot_cover_a_later_code_commit(self):
        from _lib.ci_gate import prove_failed
        from _lib.commit_gate import _test_pairs
        from _lib.gitview import Change
        legacy = {key: self.record[key] for key in ("test_file", "failed_at", "covers", "slice")}
        legacy.update(_file="docs/_cgspec/06-evidence/tests/legacy.json", test_id="legacy",
                      command=["python3", "tests/check.py", "legacy-report.json"])
        write_json(self.root / legacy["_file"], {key: value for key, value in legacy.items() if key != "_file"})
        git(self.root, "add", "docs")
        git(self.root, "commit", "-q", "-m", "docs-only legacy pair")
        self.assertIn("named runner", prove_failed(self.root, legacy))
        (self.root / "src/login/other.txt").write_text("other\n")
        git(self.root, "add", "src")
        project = json.loads((self.root / "docs/_cgspec/project.json").read_text())
        tier = {"tier": "T1", "slice": "SLICE-001"}
        reasons = _test_pairs(View.staged(self.root), [Change("M", "src/login/other.txt")], tier,
                              project["path_classes"], project)
        self.assertTrue(any("named runner" in reason for reason in reasons))

    def test_completion_record_must_match_bound_test_pair(self):
        from _lib.commit_gate import _evidence_rules
        from _lib.gitview import Change
        pair = {key: value for key, value in self.record.items() if key != "_file"}
        write_json(self.root / self.record["_file"], pair)
        path = "docs/_cgspec/06-evidence/behavior/check.json"
        completion = {key: pair[key] for key in ("runner_id", "runner_hash", "selector", "test_id")}
        completion.update(schema=1, check="check", revision="HEAD", verdict="PASS", ran_at="now", ran_by="ci",
                          claim="ui_behavior", data_source="fixture")
        write_json(self.root / path, completion)
        git(self.root, "add", "docs")
        self.assertEqual(_evidence_rules(View.staged(self.root), [Change("A", path)]), [])
        completion["runner_hash"] = "0" * 64
        write_json(self.root / path, completion)
        git(self.root, "add", "docs")
        self.assertTrue(any("does not match" in reason for reason in
                            _evidence_rules(View.staged(self.root), [Change("A", path)])))
        for field in ("runner_id", "runner_hash", "selector", "test_id"):
            completion.pop(field)
        write_json(self.root / path, completion)
        git(self.root, "add", "docs")
        self.assertTrue(any("needs runner_id" in reason for reason in
                            _evidence_rules(View.staged(self.root), [Change("A", path)])))


if __name__ == "__main__":
    unittest.main()
