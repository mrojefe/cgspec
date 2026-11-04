"""CI governance stage (spec 7b): re-check every commit in the range and prove failing-test records."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import checks, issue_linking
from .commit_gate import evaluate
from .gitview import View, git

TEST_TIMEOUT = 600

def commits_in_range(root: Path, base: str) -> list[str]:
    out = git(root, "rev-list", "--reverse", f"{base}..HEAD")
    if out.returncode != 0:
        raise RuntimeError(f"cannot list commits {base}..HEAD: {out.stderr.strip()}")
    return [c for c in out.stdout.split() if c]


def new_test_records(root: Path, base: str) -> list[dict]:
    out = git(root, "diff", "--name-only", "--diff-filter=AM", "-z", base, "HEAD", "--",
              "docs/_cgspec/06-evidence/tests").stdout
    records = []
    for rel in filter(None, out.split("\0")):
        shown = git(root, "show", f"HEAD:{rel}")
        try:
            data = json.loads(shown.stdout) if shown.returncode == 0 else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            data["_file"] = rel
            records.append(data)
    return records


def affected_test_records(root: Path, base: str) -> list[dict]:
    """Existing proof whose selected test, closure, or approved adapter inputs changed."""
    changed = set(git(root, "diff", "--name-only", base, "HEAD").stdout.splitlines())
    paths = git(root, "ls-tree", "-r", "--name-only", "HEAD", "--",
                "docs/_cgspec/06-evidence/tests").stdout.splitlines()
    project_result = git(root, "show", "HEAD:docs/_cgspec/project.json")
    try:
        project = json.loads(project_result.stdout) if project_result.returncode == 0 else {}
    except ValueError:
        project = {}
    old_project_result = git(root, "show", f"{base}:docs/_cgspec/project.json")
    try:
        old_project = json.loads(old_project_result.stdout) if old_project_result.returncode == 0 else {}
    except ValueError:
        old_project = {}
    if not isinstance(old_project, dict):
        old_project = {}
    project_test_changed = any(old_project.get(k) != project.get(k) for k in ("test_command", "test_runners", "test_environments"))
    from . import gates
    code_changed = {p for p in changed if gates.needs_tier(p) and not p.startswith(".agents/skills/")}
    runner_inputs = {}
    raw_environments = project.get("test_environments", [])
    environments = {item.get("id"): item for item in raw_environments if isinstance(item, dict)} \
        if isinstance(raw_environments, list) else {}
    raw_runners = project.get("test_runners", [])
    for runner in raw_runners if isinstance(raw_runners, list) else []:
        if not isinstance(runner, dict):
            continue
        environment = environments.get(runner.get("environment"), {})
        semantics = runner.get("semantics") if isinstance(runner.get("semantics"), dict) else {}
        environment_inputs = environment.get("inputs", []) if isinstance(environment.get("inputs"), list) else []
        semantic_inputs = semantics.get("inputs", []) if isinstance(semantics.get("inputs"), list) else []
        runner_inputs[runner.get("id")] = set(environment_inputs) | set(semantic_inputs)
    records = []
    for rel in paths:
        shown = git(root, "show", f"HEAD:{rel}")
        try:
            record = json.loads(shown.stdout) if shown.returncode == 0 else None
        except ValueError:
            record = None
        if not isinstance(record, dict):
            continue
        semantics = record.get("test_semantics") if isinstance(record.get("test_semantics"), dict) else {}
        raw_dependencies = semantics.get("dependencies")
        dependencies = {item.get("path") for item in raw_dependencies
                        if isinstance(item, dict) and isinstance(item.get("path"), str)} if isinstance(raw_dependencies, list) else set()
        watched = {record.get("test_file")} | dependencies | runner_inputs.get(record.get("runner_id"), set())
        from .test_evolution import known_closure
        unknown_closure = not known_closure(semantics)
        if (unknown_closure and code_changed) or project_test_changed or changed & watched:
            record["_file"] = rel
            records.append(record)
    return records


def _proof_superseded(root: Path, proof_path: str) -> bool:
    """Retire proof only after an approved historical transition and fresh replacement replay."""
    from . import run_supersession
    candidate = git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    if not candidate:
        return False
    view = View.commit(root, candidate)
    if proof_path not in run_supersession.retired_proofs(view):
        return False
    for path in view.files_new("docs/_cgspec/05-runs"):
        if not path.endswith("/run.json"):
            continue
        old = run_supersession._data(view, path)
        if not old or old.get("status") != "superseded" or proof_path not in (run_supersession._refs(old, "proofs") or []):
            continue
        replacement = run_supersession._data(view, f"docs/_cgspec/05-runs/{old.get('superseded_by')}/run.json")
        if not replacement or run_supersession.errors(old, replacement) or run_supersession.proof_link_errors(old, replacement, view.read_new):
            continue
        targets = [link["new"] for link in replacement["supersession"]["links"]["proofs"] if link["old"] == proof_path]
        if len(targets) != 1:
            continue
        record = run_supersession._data(view, targets[0])
        record["_file"] = targets[0]
        if prove_failed(root, record) is None and git(root, "rev-parse", "HEAD").stdout.strip() == candidate:
            return True
    return False


def _run_at(root: Path, rev: str, command: list) -> int | str:
    """Exit code of command in a throwaway worktree at rev, or an error message."""
    tmp = Path(tempfile.mkdtemp(prefix="cgspec-proof-"))
    tree = tmp / "tree"
    try:
        if git(root, "worktree", "add", "--detach", "-q", str(tree), rev).returncode != 0:
            return f"{rev} cannot be checked out"
        try:
            return subprocess.run(command, cwd=tree, capture_output=True, timeout=TEST_TIMEOUT).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not run at {rev[:10]}: {exc}"
    finally:
        git(root, "worktree", "remove", "--force", str(tree))
        shutil.rmtree(tmp, ignore_errors=True)


def prove_failed(root: Path, record: dict) -> str | None:
    """The recorded test must fail at failed_at and pass at HEAD."""
    command, name = record.get("command"), record["_file"]
    if not isinstance(command, list) or not command:
        return f"{name}: no command to re-run"
    failed_at, test_file = str(record.get("failed_at", "")), str(record.get("test_file", ""))
    if git(root, "cat-file", "-e", f"{failed_at}:{test_file}").returncode != 0:
        from .proof_seals import historical_pair_error
        candidate = git(root, "rev-parse", "HEAD").stdout.strip()
        if historical_pair_error(View.commit(root, candidate), record["_file"], record) is None:
            return None
        return f"{name}: test file {test_file} did not exist at {failed_at[:10]}, so it cannot have failed there"
    if any(field in record for field in ("runner_id", "runner_hash", "selector")):
        return prove_named(root, record)
    if git(root, "diff", "--quiet", failed_at, "HEAD", "--", test_file).returncode != 0:
        return f"{name}: test file {test_file} was modified after failed_at {failed_at[:10]}; a failing test cannot be weakened to make it pass"
    shown = git(root, "show", "HEAD:docs/_cgspec/project.json")
    try:
        project = json.loads(shown.stdout) if shown.returncode == 0 else {}
    except ValueError:
        project = {}
    if isinstance(project, dict) and project.get("test_runners"):
        return f"{name}: named runner and assertion-aware proof are required after migration"
    before = _run_at(root, record.get("failed_at", ""), command)
    if isinstance(before, str):
        return f"{name}: {before}"
    if before == 0:
        return f"{name}: test {record.get('test_id')} did not fail at {str(record.get('failed_at'))[:10]}; the record is not proof"
    after = _run_at(root, "HEAD", command)
    if isinstance(after, str):
        return f"{name}: {after}"
    if after != 0:
        return f"{name}: test {record.get('test_id')} still fails at HEAD"
    return None


def prove_named(root: Path, record: dict) -> str | None:
    from . import runner_contracts, runner_execution
    from .common import CgspecError
    name = record["_file"]
    candidate = git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if candidate.returncode:
        return f"{name}: cannot resolve candidate HEAD"
    candidate_sha = candidate.stdout.strip()
    definitions = []
    for rev in (record["failed_at"], candidate_sha):
        result = git(root, "show", f"{rev}:docs/_cgspec/project.json")
        try:
            project = json.loads(result.stdout) if result.returncode == 0 else {}
        except ValueError:
            project = {}
        if not isinstance(project, dict) or not project.get("test_runners"):
            return f"{name}: approved runner was not present at {rev[:10]}"
        reader = lambda rel: (result.stdout if (result := git(root, "show", f"{rev}:{rel}", binary=True)).returncode == 0 else None)
        problems = runner_contracts.pair_errors(project, record, reader)
        if problems:
            return f"{name}: {', '.join(problems)} at {rev[:10]}"
        try:
            runner, environment, _ = runner_contracts.resolve(project, record["runner_id"], reader)
        except CgspecError as exc:
            return f"{name}: {exc}"
        definitions.append((runner, environment))

    from .test_evolution import preservation_error
    problem = preservation_error(root, record["failed_at"], *definitions[0], record["test_file"],
                                 record["test_id"], record.get("test_semantics"), candidate_sha)
    if problem:
        return f"{name}: {problem}"
    proof_dir = Path(os.environ.get("CGSPEC_PROOF_DIR", str(root / ".cgspec-scratch/ci-proof")))
    for label, rev, definition, expected in (("red", record["failed_at"], definitions[0], "ASSERTION_FAILED"),
                                              ("green", candidate_sha, definitions[1], "PASS")):
        detail = runner_execution.run_detail(root, rev, *definition, record["test_file"])
        detail.update(proof_file=name, test_id=record.get("test_id"), phase=label, candidate_revision=candidate_sha)
        # Names are derived from trusted hashes rather than an untrusted test_id or filename.
        import hashlib
        key = hashlib.sha256(name.encode()).hexdigest()[:20]
        try:
            proof_dir.mkdir(parents=True, exist_ok=True)
            (proof_dir / f"{key}-{label}.json").write_text(json.dumps(detail, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            return f"{name}: cannot retain structured proof: {exc}"
        if detail["result"] != expected:
            return f"{name}: {label} run was {detail['result']}, not {expected} at {rev[:10]}"
    return None


def install_commit(root: Path) -> str | None:
    out = git(root, "log", "--diff-filter=A", "--format=%H", "--", "docs/_cgspec/project.json").stdout.split()
    return out[-1] if out else None


def effective_base(root: Path, base: str | None) -> str:
    """Never judge history from before CGSpec was installed; fall back to the install commit."""
    installed = install_commit(root)
    valid = bool(base) and git(root, "cat-file", "-e", f"{base}^{{commit}}").returncode == 0
    if not valid:
        if not installed:
            raise RuntimeError("no valid base and no install commit found")
        return installed
    if installed and git(root, "merge-base", "--is-ancestor", installed, base).returncode != 0:
        return installed  # base predates the install
    return base


def check_issue_linking(root: Path, base: str, pr: str | None,
                        require_issue: bool | None) -> tuple[list[str], list[str]]:
    pj_file = root / "docs" / "_cgspec" / "project.json"
    project_data = {}
    if pj_file.exists():
        try:
            project_data = json.loads(pj_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            project_data = {}
    if not isinstance(project_data, dict):
        project_data = {}

    is_ci_pr = (
        os.environ.get("GITHUB_EVENT_NAME") == "pull_request"
        and (
            not os.environ.get("GITHUB_WORKSPACE")
            or Path(os.environ["GITHUB_WORKSPACE"]).resolve() == root.resolve()
        )
    )
    enforce_issue = (
        pr is not None
        or bool(require_issue)
        or bool(project_data.get("require_issue_linking"))
        or os.environ.get("CGSPEC_REQUIRE_ISSUE") == "1"
        or is_ci_pr
    )
    if not enforce_issue:
        return [], []
    repository = issue_linking.repository(root) or ""
    references = issue_linking.references(root, commits_in_range(root, base), repository, pr)
    if not references:
        return ["candidate PR or commit range has no associated issue reference (e.g. closes #<id>, fixes #<id>, or #<id>)"], []
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return ["cannot verify issue linking: GitHub repository identity is unavailable"], []
    credential = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not credential:
        return ["cannot verify issue linking: GH_TOKEN or GITHUB_TOKEN with issues: read is required"], []
    try:
        if any(issue_linking.existing_issue(repository, number, credential) for number in sorted(references)):
            return [], ["verified PR-to-issue reference against an existing issue"]
    except RuntimeError as exc:
        return [f"cannot verify issue linking: {exc}; check issues: read permission and API access"], []
    return [f"no referenced existing issue in {repository} (references: {', '.join('#' + str(n) for n in sorted(references))})"], []


def run(root: Path, base: str | None, rerun_tests: bool = True,
        release_dir: Path | None = None,
        pr: str | None = None,
        require_issue: bool | None = None) -> tuple[list[str], list[str]]:
    """(problems, notes) for the range base..HEAD."""
    problems, notes = [], []
    base = effective_base(root, base)
    notes.append(f"checking {base[:10]}..HEAD")

    issue_problems, issue_notes = check_issue_linking(root, base, pr, require_issue)
    # Later checks and proof commands execute candidate code. They must not inherit the API token.
    os.environ.pop("GH_TOKEN", None)
    os.environ.pop("GITHUB_TOKEN", None)
    problems += issue_problems
    notes += issue_notes

    for commit in commits_in_range(root, base):
        subject = git(root, "log", "-1", "--format=%h %s", commit).stdout.strip()
        view = View.commit(root, commit)
        reasons = evaluate(root, view, release_dir, strict=True)
        reasons += [f"check integrity: {problem}" for problem in checks.verify_manifest_at(view)]
        problems += [f"commit {subject}: {reason}" for reason in reasons]
    changed = [line for line in git(root, "diff", "--name-only", "--diff-filter=d", f"{base}..HEAD").stdout.splitlines()
               if line]
    # The same trusted range base the commits above were judged against (the workflow's CGSPEC_BASE).
    failures, check_notes = checks.run(root, changed, write_evidence=False, base_rev=base, repo_root=root)
    problems += [f"check {failure}" for failure in failures]
    notes += [note for note in check_notes if not note.startswith("no registered")]
    if not rerun_tests:
        problems.append("incomplete verification: --no-rerun skipped failing-test proof; "
                        "this result cannot satisfy the governance check")
    else:
        new_records = new_test_records(root, base)
        for record in new_records:
            problem = prove_failed(root, record)
            if problem:
                problems.append(problem)
            else:
                label = "legacy exit-code-only proof" if "runner_id" not in record else "structured runner proof"
                notes.append(f"proved ({label}): {record.get('test_id')} failed at {str(record.get('failed_at'))[:10]}")
                notes.append(f"proved: {record.get('test_id')} failed at {str(record.get('failed_at'))[:10]}")
        new_paths = {record["_file"] for record in new_records}
        for record in affected_test_records(root, base):
            if record["_file"] in new_paths:
                continue
            if _proof_superseded(root, record["_file"]):
                notes.append(f"historical proof superseded: {record.get('test_id')}")
                continue
            problem = (f"{record['_file']}: unknown dependency closure requires fresh semantic proof"
                       if not record.get("test_semantics") else prove_failed(root, record))
            if problem:
                problems.append(problem)
            else:
                notes.append(f"preserved proof: {record.get('test_id')}")
    return problems, notes
