"""Verify finished approvals locally before Git can publish them.

commit-msg runs before Git creates the signed object, so it cannot verify that
signature. pre-push is blocking and checks every outgoing commit, including
intermediate commits, against that commit's own parent policy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import commit_trust as trust
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


def _find_test_files(root: Path, commits: set[str], test_globs: list[str]) -> list[str]:
    touched: list[str] = []
    for sha in sorted(commits):
        view = View.commit(root, sha)
        for c in view.changes():
            if c.status != "D" and match_any(c.path, test_globs) and (root / c.path).is_file():
                if c.path not in touched:
                    touched.append(c.path)
    if not touched:
        for folder in ("tests", "test", "spec", "specs"):
            tdir = root / folder
            if tdir.is_dir():
                for p in sorted(tdir.rglob("*")):
                    if p.is_file() and not p.name.startswith((".", "_", "helper", "conftest")):
                        rel = p.relative_to(root).as_posix()
                        if match_any(rel, test_globs):
                            if rel not in touched:
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


def _run_runners(root: Path, test_runners: list, commits: set[str], test_globs: list[str]) -> list[str]:
    failures = []
    for runner in test_runners:
        if not isinstance(runner, dict):
            continue
        cmd = runner.get("command")
        if not (isinstance(cmd, list) and cmd):
            continue
        runner_cwd = root / runner.get("cwd", ".")
        timeout = runner.get("timeout_seconds", 300)
        runner_id = runner.get("id", "default")
        globs = runner.get("applies_to") if isinstance(runner.get("applies_to"), list) else test_globs

        if "{test_file}" in cmd:
            files = _find_test_files(root, commits, globs)
            if not files:
                failures.append(f"test runner {runner_id} uses {{test_file}}, but no test files were found")
            for tf in files:
                sub_cmd = [tf if part == "{test_file}" else part for part in cmd]
                try:
                    res = subprocess.run(sub_cmd, cwd=runner_cwd, capture_output=True, text=True, timeout=timeout)
                    if res.returncode != 0:
                        detail = (res.stderr or res.stdout or "").strip()
                        failures.append(f"test runner {runner_id} on {tf} failed (exit {res.returncode}): {detail}" if detail else f"test runner {runner_id} on {tf} failed (exit {res.returncode})")
                except Exception as exc:
                    failures.append(f"could not run test runner {runner_id} on {tf} ({sub_cmd}): {exc}")
        else:
            try:
                res = subprocess.run(cmd, cwd=runner_cwd, capture_output=True, text=True, timeout=timeout)
                if res.returncode != 0:
                    detail = (res.stderr or res.stdout or "").strip()
                    failures.append(f"test runner {runner_id} failed (exit {res.returncode}): {detail}" if detail else f"test runner {runner_id} failed (exit {res.returncode})")
            except Exception as exc:
                failures.append(f"could not run test runner {runner_id} ({cmd}): {exc}")
    return failures


def run_pre_push_tests(root: Path, commits: set[str]) -> list[str]:
    pj_file = root / "docs" / "_cgspec" / "project.json"
    if not pj_file.exists():
        return []
    try:
        project = json.loads(pj_file.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(project, dict):
        return []
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
    elif isinstance(test_runners, list) and test_runners:
        failures.extend(_run_runners(root, test_runners, commits, test_globs))

    return failures


def pre_push(root: Path, raw: str) -> int:
    commits, reasons = set(), []
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
        test_failures = run_pre_push_tests(root, commits)
        if test_failures:
            print("CGSpec pre-push test run failed (push blocked):\n- " + "\n- ".join(test_failures))
            return EXIT_FAIL
    print("CGSpec local approval check passed")
    return EXIT_OK
