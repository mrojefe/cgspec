"""Test helpers: import path for the engine and throwaway project trees."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

PY = sys.executable


def run_script(script: str, *args: str, cwd: Path | None = None, stdin: str | None = None, env: dict | None = None):
    return subprocess.run(
        [PY, str(ENGINE / script), *args],
        cwd=cwd, input=stdin, capture_output=True, text=True, timeout=60, env=env,
    )


class TempDir:
    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="cgspec-test-"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def snapshot_tree(root: Path) -> dict[str, bytes]:
    """Map of every file (relative path) to its bytes, ignoring caches."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def install(root: Path, profile: str = "web-workspace", topology: str = "single", *extra: str, env: dict | None = None):
    env = dict(os.environ if env is None else env)
    env.setdefault("CLAUDE_CONFIG_DIR", str(root / ".test-claude-config"))
    return run_script("install.py", "install", "--root", str(root), "--profile", profile,
                      "--topology", topology, "--tools", "claude,git", *extra, env=env)


def doctor(root: Path, *extra: str):
    return run_script("doctor.py", "--root", str(root), *extra)


def hook(root: Path, event: str, payload: dict | None = None, *extra: str,
         env: dict | None = None):
    import json as _json
    return run_script("cgspec_hook.py", event, "--root", str(root), *extra,
                      stdin=_json.dumps(payload) if payload is not None else "", env=env)


def claude_payload(root: Path, tool: str, **tool_input) -> dict:
    for key in ("file_path", "notebook_path"):
        if key in tool_input and not str(tool_input[key]).startswith("/"):
            tool_input[key] = str(root / tool_input[key])
    return {"session_id": "s1", "cwd": str(root), "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": tool_input}


def git(root: Path, *args: str, check: bool = True):
    result = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.com",
                             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                            cwd=root, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def git_live(root: Path, *args: str, check: bool = True):
    """Run real git with the installed hooks active: no hooksPath override, no --no-verify,
    and isolated from the host's own global/system config so hook-behaviour assertions mean
    something (a bare `git` helper call always disables hooks via core.hooksPath=/dev/null)."""
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    result = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.com",
                             "-c", "commit.gpgsign=false", *args],
                            cwd=root, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stdout}{result.stderr}")
    return result


def git_project(root: Path, profile: str = "web-workspace") -> None:
    """Installed project in a git repo with one initial commit."""
    git(root, "init", "-q", "-b", "main")
    install(root, profile)
    (root / "src").mkdir(exist_ok=True)
    (root / "src/a.ts").write_text("export const a = 1;\n")
    (root / "src/login").mkdir(exist_ok=True)
    (root / "src/login/form.ts").write_text("export const f = 1;\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "checkout", "-q", "-b", "codex/test-work")


def approve(root: Path, text: str = "approved, go ahead") -> str:
    """Record an approval the way the user-message hook does and return its id."""
    import json as _json
    user_message(root, text)
    data = _json.loads((root / "docs/_cgspec/07-state/approvals.json").read_text())
    return data["approvals"][-1]["id"]


def user_message(root: Path, prompt: str):
    """Host-event fixture: mock only OS peer identity, never a production bypass flag."""
    import argparse
    import contextlib
    import io
    from unittest.mock import patch
    from cgspec_hook import lifecycle
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": prompt,
               "session_id": "550e8400-e29b-41d4-a716-446655440000", "cwd": str(root)}
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch("_lib.hook_origin._peer_and_image", return_value=(1234, Path("/trusted/claude"))), \
            patch("_lib.hook_origin._claude_launcher", return_value=Path("/trusted/claude")), \
            patch("sys.stdin", io.StringIO(json.dumps(payload))) as stdin, \
            patch.object(stdin, "fileno", return_value=0), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = lifecycle(root, "user-message", argparse.Namespace(tool="claude"))
    return subprocess.CompletedProcess(["UserPromptSubmit"], code, stdout.getvalue(), stderr.getvalue())


def make_slice(root: Path, status: str = "Approved", scope: str = "src/login/**", approval: str | None = None,
               number: str = "001", index: bool = True) -> Path:
    from _lib.document_dates import stamp_new, touch
    approval = approval if approval is not None else approve(root, f"approve SLICE-{number}")
    path = root / f"docs/slices/SLICE-{number}-login.md"
    lines = [f"# SLICE-{number}: Login", "", f"- **Status:** {status}", f"- **Scope:** {scope}"]
    if approval:
        lines.append(f"- **Approval:** {approval}")
    from _lib.common import utc_now
    path.write_text(stamp_new("\n".join(lines) + "\n", utc_now()))
    idx = root / "docs/00-INDEX.md"
    entry = f"- [SLICE-{number}](slices/{path.name})"
    if index and entry not in idx.read_text():
        idx.write_text(touch(idx.read_text() + "\n" + entry + "\n", utc_now()))
    return path


def make_git_plan(root: Path, slice_id: str = "SLICE-001", outcome_id: str = "OUT-work",
                  branch: str | None = None) -> None:
    """Declare a minimal git execution plan for a slice so implementation gates pass."""
    branch = branch or git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    project = json.loads((root / "docs/_cgspec/project.json").read_text())
    if branch in project.get("protected_branches", []):
        branch = f"codex/{slice_id.lower()}"
        git(root, "checkout", "-q", "-b", branch)
    base = git(root, "rev-parse", "HEAD").stdout.strip()
    draft = {
        "repositories": [{
            "id": "main", "base": base, "branch": branch,
            "worktree": None, "outcomes": [outcome_id], "file_ownership": {},
        }],
        "default_branch": branch,
    }
    source = root / ".cgspec-scratch/plan-draft.json"
    source.parent.mkdir(exist_ok=True)
    write_json(source, draft)
    result = run_script("cgspec_hook.py", "git-plan", "--root", str(root),
                        "declare", "--slice", slice_id, "--file", str(source))
    if result.returncode:
        raise AssertionError(result.stderr)


def make_ready_start(root: Path, slice_id: str = "SLICE-001", outcome_id: str = "OUT-work",
                     with_git_plan: bool = True) -> None:
    """Give legacy gate fixtures a valid approved outcome; #30 tests exercise the workflow explicitly."""
    from _lib import tiers
    found = tiers.find_slice(root, slice_id)
    if not found:
        raise AssertionError(f"missing slice fixture {slice_id}")
    definition = {
        "goal": "Exercise the declared behavior with explicit proof",
        "outcomes": [{
            "id": outcome_id, "statement": "The scoped behavior is demonstrated",
            "covers": found[2], "accept": ["The declared check passes"],
            "reject": ["Unrelated or failing behavior is not accepted"],
            "evaluation": {"method": "Run the scoped project checks", "requires_real": True,
                           "data_source": "real_backend", "proof": ["logs"]},
            "development": {"uses_mocks": False, "data_source": "real_backend"},
        }],
        "exclusions": [], "constraints": [],
        "design": {"required": False, "target": None, "states": []}, "prerequisites": [],
    }
    source = root / ".cgspec-scratch/legacy-start.json"
    source.parent.mkdir(exist_ok=True)
    write_json(source, definition)
    base = ("cgspec_hook.py", "start", "--root", str(root), "--slice", slice_id)
    for args in (("set", "--file", str(source)),):
        result = run_script(*base, *args)
        if result.returncode:
            raise AssertionError(result.stderr)
    ref = approve(root, f"approve {slice_id.split(':')[-1]}")
    for args in (("agree", "--approval", ref), ("ready",), ("begin", "--outcome", outcome_id)):
        result = run_script(*base, *args)
        if result.returncode:
            raise AssertionError(result.stderr)
    if with_git_plan:
        make_git_plan(root, slice_id, outcome_id)


def set_test_command(root: Path, command: list[str]) -> None:
    """Set project.json test_command with a fresh approval from the user, ready to commit."""
    import json as _json
    import os as _os
    ref = approve(root, "approve the test command")
    pj = root / "docs/_cgspec/project.json"
    _os.chmod(pj, 0o644)
    data = _json.loads(pj.read_text())
    data.update(test_command=command, test_command_approval=ref)
    write_json(pj, data)
