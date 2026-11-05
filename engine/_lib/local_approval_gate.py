"""Verify finished approvals locally before Git can publish them.

commit-msg runs before Git creates the signed object, so it cannot verify that
signature. pre-push is blocking and checks every outgoing commit, including
intermediate commits, against that commit's own parent policy.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import commit_trust as trust
from . import runner_contracts, runner_execution, schema
from .common import EXIT_FAIL, EXIT_OK
from .gitview import View, git
from .globs import match_any


def commit_problems(view: View) -> list[str]:
    reasons = trust.append_only(view) + trust.approvals_have_inbox_lines(view) + trust.approver_key_change(view)
    message = trust.commit_message(view) or ""
    if trust.TRAILER.search(message) or trust.staged_trailer_refs(view) or trust.governance_change(view):
        if not trust.TRAILER.search(message):
            reasons.append("approval requires an Approved-by: trailer written by the human")
        if not trust.trusted_signature(view):
            reasons.append("approval commit is not signed by a key in the parent's project.json approver_keys")
    return reasons


def post_commit(root: Path) -> int:
    """Check the newly-created commit object immediately after commit creation."""
    import sys
    view = View.commit(root, "HEAD")
    if trust.is_bootstrap(view):
        return EXIT_OK
    reasons = commit_problems(view)
    if reasons:
        sha = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
        print(f"CGSpec local approval check failed ({sha}):\n- " + "\n- ".join(reasons), file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def _find_test_files(root: Path, commits: set[str], test_globs: list[str],
                     explicit_globs: list[str] | None = None) -> list[str]:
    def candidate(rel: str) -> bool:
        return (not rel.startswith("docs/_cgspec/01-engine/")
                and match_any(rel, test_globs) and (root / rel).is_file())

    def implicit_candidate(rel: str) -> bool:
        excluded = (".", "_", "helper", "conftest")
        if not Path(rel).name.startswith(excluded):
            return True
        selectors = test_globs + (explicit_globs or [])
        return any(Path(pattern).name.startswith(excluded) and match_any(rel, [pattern])
                   for pattern in selectors)

    touched: list[str] = []
    for sha in sorted(commits):
        view = View.commit(root, sha)
        for c in view.changes():
            if c.status != "D" and candidate(c.path) and implicit_candidate(c.path):
                if c.path not in touched:
                    touched.append(c.path)
    if not touched:
        tracked = git(root, "ls-files", "-z")
        if tracked.returncode:
            return []
        for rel in sorted(filter(None, tracked.stdout.split("\0"))):
            if candidate(rel) and implicit_candidate(rel):
                touched.append(rel)
    return touched


def _run_test_cmd(root: Path, commits: set[str], test_cmd: list[str], test_globs: list[str]) -> list[str]:
    failures = []
    if "{test_file}" in test_cmd:
        files = _find_test_files(root, commits, test_globs)
        if not files:
            return ["test command uses {test_file}, but no test files were found"]
        for tf in files:
            cmd = [tf if part == "{test_file}" else part for part in test_cmd]
            try:
                res = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=300)
                if res.returncode != 0:
                    detail = (res.stderr or res.stdout or "").strip()
                    failures.append(f"test {tf} failed (exit {res.returncode}): {detail}" if detail else f"test {tf} failed (exit {res.returncode})")
            except Exception as exc:
                failures.append(f"could not run test {tf} ({cmd}): {exc}")
    else:
        try:
            res = subprocess.run(test_cmd, cwd=root, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                detail = (res.stderr or res.stdout or "").strip()
                failures.append(f"test command {' '.join(test_cmd)} failed (exit {res.returncode}): {detail}" if detail else f"test command {' '.join(test_cmd)} failed (exit {res.returncode})")
        except Exception as exc:
            failures.append(f"could not run test command {' '.join(test_cmd)}: {exc}")
    return failures


def _run_runners(repo: Path, root: Path, revision: str, project: dict,
                 commits: set[str], test_globs: list[str]) -> list[str]:
    failures = []
    contract_errors = runner_contracts.errors(project)
    if contract_errors:
        return [f"invalid test runner contract: {problem}" for problem in contract_errors]
    runners = project["test_runners"]
    if len(runners) > 1 and any(not runner.get("test_globs") for runner in runners):
        return ["multiple named test runners must each declare non-empty test_globs"]
    explicit_globs = [pattern for runner in runners for pattern in runner.get("test_globs", [])]
    files = _find_test_files(root, commits, test_globs, explicit_globs)
    if not files:
        return ["named test runners select no test files from path_classes.tests"]
    selected = {runner["id"]: [test_file for test_file in files
                               if match_any(test_file, runner.get("test_globs", test_globs))]
                for runner in runners}
    unassigned = [test_file for test_file in files
                  if not any(test_file in runner_files for runner_files in selected.values())]
    if unassigned:
        return ["named test runners do not select test files: " + ", ".join(unassigned)]
    for runner in runners:
        environment = next(item for item in project["test_environments"]
                           if item["id"] == runner["environment"])
        for test_file in selected[runner["id"]]:
            detail = runner_execution.run_detail(repo, revision, runner, environment, test_file)
            if detail["result"] != "PASS":
                reason = detail.get("reason") or detail["result"]
                failures.append(f"test runner {runner['id']} on {test_file} failed: {reason}")
    return failures


def _run_at_revision(repo: Path, revision: str, commits: set[str]) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="cgspec-pre-push-") as directory:
        tree = Path(directory) / "tree"
        result = git(repo, "worktree", "add", "--detach", "-q", str(tree), revision)
        if result.returncode:
            return [f"cannot check out outgoing revision {revision[:12]} for pre-push tests"]
        try:
            failures = run_pre_push_tests(repo, tree, revision, commits)
        finally:
            removed = git(repo, "worktree", "remove", "--force", str(tree))
        if removed.returncode:
            failures.append(f"cannot remove pre-push test worktree for {revision[:12]}")
        return failures


def run_pre_push_tests(repo: Path, root: Path, revision: str, commits: set[str]) -> list[str]:
    pj_file = root / "docs" / "_cgspec" / "project.json"
    if not pj_file.exists():
        return ["outgoing revision has no docs/_cgspec/project.json; only an installation bootstrap that introduces it can establish test policy"]
    try:
        project = json.loads(pj_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return ["outgoing docs/_cgspec/project.json is unreadable or malformed"]
    problems = schema.validate_named(project, "project")
    if problems:
        return [f"outgoing docs/_cgspec/project.json is invalid: {problem}" for problem in problems]
    if project.get("pre_push_tests") is False:
        return []

    test_cmd, test_runners = project.get("test_command"), project.get("test_runners")
    if not test_cmd and not test_runners:
        return []

    failures = []
    classes = project.get("path_classes", {}) if isinstance(project.get("path_classes"), dict) else {}
    test_globs = classes.get("tests", ["tests/**"]) if isinstance(classes.get("tests"), list) else ["tests/**"]

    if isinstance(test_cmd, list) and test_cmd:
        failures.extend(_run_test_cmd(root, commits, test_cmd, test_globs))
    if isinstance(test_runners, list) and test_runners:
        failures.extend(_run_runners(repo, root, revision, project, commits, test_globs))

    return failures


def pre_push(root: Path, raw: str) -> int:
    commits, reasons, outgoing = set(), [], {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 4 or any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[i]) for i in (1, 3)):
            reasons.append("invalid pre-push ref update; cannot establish outgoing commits")
            continue
        local, remote = fields[1], fields[3]
        if set(local) == {"0"}:
            continue  # ref deletion has no outgoing approvals
        args = ["rev-list", local]
        if set(remote) != {"0"}:
            args.append("^" + remote)
        result = git(root, *args)
        if result.returncode:
            reasons.append("cannot establish outgoing commit range; fetch the remote history and retry")
            continue
        outgoing.setdefault(local, set()).update(result.stdout.splitlines())
        commits.update(result.stdout.splitlines())
    for sha in sorted(commits):
        view = View.commit(root, sha)
        # The initial installation baseline needs separate human review; it has
        # no earlier key policy that any local/CI signature check could trust.
        if trust.is_bootstrap(view):
            continue
        reasons += [f"{sha[:12]}: {problem}" for problem in commit_problems(view)]
    if reasons:
        print("CGSpec local approval check failed (push blocked):\n- " + "\n- ".join(reasons))
        return EXIT_FAIL
    if commits:
        test_failures = []
        for revision, revision_commits in outgoing.items():
            if revision_commits:
                test_failures.extend(_run_at_revision(root, revision, revision_commits))
        if test_failures:
            print("CGSpec pre-push test run failed (push blocked):\n- " + "\n- ".join(test_failures))
            return EXIT_FAIL
    print("CGSpec local approval check passed")
    return EXIT_OK
