"""Wire the hook into each AI tool, git and CI (spec 8 per-tool table), and unwire on uninstall.

Every change is recorded in owned-files.json under "adapters" so uninstall removes exactly
what install added and leaves the user's own settings and hooks alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

from .adapter_templates import CURSOR_RULE, GITHUB_STEPS, SELECT, WORKFLOW
from .ci_visual import VISUAL_WORKFLOW
from .common import CgspecError, STATE_DIR, utc_now, write_json
from . import claude_statusline, git_hooks

HOOK = "docs/_cgspec/01-engine/cgspec_hook.py"
MARK = "cgspec_hook.py"

CLAUDE_EVENTS = {
    "PreToolUse": ("Write|Edit|MultiEdit|NotebookEdit|Bash", "pre-tool"),
    "PostToolUse": (None, "post-tool"),
    "UserPromptSubmit": (None, "user-message"),
    "SessionStart": (None, "session-start"),
    "PreCompact": (None, "pre-compact"),
    "Stop": (None, "stop"),
}
AG_EVENTS = {
    "PreToolUse": ("write_to_file|replace_file_content|multi_replace_file_content|client_edit_file|client_create_file|run_command", "pre-tool"),
    "PostToolUse": ("write_to_file|replace_file_content|multi_replace_file_content|client_edit_file|client_create_file|run_command|invoke_subagent|call_mcp_tool", "post-tool"),
    "SessionStart": (None, "session-start"),
    "Stop": (None, "stop"),
}
CODEX_EVENTS = {
    "PreToolUse": ("apply_patch|file_edit|write_file|str_replace|bash|Bash|exec_command", "pre-tool"),
    "PostToolUse": ("apply_patch|file_edit|write_file|str_replace|bash|Bash|exec_command", "post-tool"),
    "UserPromptSubmit": (None, "user-message"),
    "SessionStart": (None, "session-start"),
    "Stop": (None, "stop"),
}
OPENCODE_EVENTS = {
    "PreToolUse": ("edit|write|bash|file_edit|patch", "pre-tool"),
    "PostToolUse": ("edit|write|bash|file_edit|patch", "post-tool"),
    "SessionStart": (None, "session-start"),
    "Stop": (None, "stop"),
}
GIT_HOOK = """#!/bin/sh
# Installed by CGSpec. Remove with: python3 docs/_cgspec/01-engine/install.py uninstall
exec python3 "$(git rev-parse --show-toplevel)/{hook}" {event} --tool git "$@"
"""


def _command(event: str, tool: str) -> str:
    if tool == "claude":
        base = '"${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"/'
    elif tool == "antigravity":
        # Antigravity starts hooks in .agents/ on some hosts, not the repo root.
        # A Git root handles nested working directories; the fallback covers installs before git init.
        base = '"$(git rev-parse --show-toplevel 2>/dev/null || { [ -f ' + HOOK + ' ] && pwd || (cd .. && pwd); })"/'
    else:
        base = '"$(git rev-parse --show-toplevel 2>/dev/null || pwd)"/'
    return f"python3 {base}{HOOK} {event} --tool {tool}"


def _merge_json(path: Path, add) -> bool:
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return False  # never overwrite a file we cannot parse
    created = not path.exists()
    add(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    return created


def _claude(data: dict) -> None:
    hooks = data.setdefault("hooks", {})
    for event, (matcher, name) in CLAUDE_EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": _command(name, "claude")}]}
        if matcher:
            entry["matcher"] = matcher
        if MARK not in json.dumps(hooks.get(event, [])):
            hooks.setdefault(event, []).append(entry)


def _antigravity(data: dict) -> None:
    block = {}
    for event, (matcher, name) in AG_EVENTS.items():
        handler = {"type": "command", "command": _command(name, "antigravity"), "timeout": 30}
        block[event] = [{"matcher": matcher, "hooks": [handler]}] if matcher else [handler]
    data["cgspec"] = block


def _strip_hook_entries(data: dict) -> None:
    target = data.get("hooks") if isinstance(data.get("hooks"), dict) else data
    for event in list(target):
        if isinstance(target.get(event), list):
            target[event] = [e for e in target[event] if MARK not in json.dumps(e)]
            if not target[event]:
                del target[event]
    if isinstance(data.get("hooks"), dict) and not data["hooks"]:
        data.pop("hooks", None)


def _codex(data: dict) -> None:
    target = data.get("hooks") if isinstance(data.get("hooks"), dict) else data
    for event, (matcher, name) in CODEX_EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": _command(name, "codex"), "timeout": 30}]}
        if matcher:
            entry["matcher"] = matcher
        if MARK not in json.dumps(target.get(event, [])):
            target.setdefault(event, []).append(entry)


def _opencode(data: dict) -> None:
    target = data.get("hooks") if isinstance(data.get("hooks"), dict) else data
    for event, (matcher, name) in OPENCODE_EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": _command(name, "opencode"), "timeout": 30}]}
        if matcher:
            entry["matcher"] = matcher
        if MARK not in json.dumps(target.get(event, [])):
            target.setdefault(event, []).append(entry)


def _write_new(root: Path, rel: str, text: str, mode: int | None = None) -> tuple[bool, str]:
    path = root / rel
    if path.exists() and MARK not in path.read_text(encoding="utf-8", errors="replace") and "CGSpec" not in path.read_text(encoding="utf-8", errors="replace"):
        return False, f"left existing {rel} alone; add the CGSpec call to it yourself"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode:
        os.chmod(path, mode)
    return True, f"wired {rel}"


def _freeze_workflow(root: Path, rel: str) -> None:
    frozen_path = root / STATE_DIR / "frozen.json"
    data = json.loads(frozen_path.read_text(encoding="utf-8"))
    if rel not in [e.get("path") for e in data["entries"]]:
        digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        data["entries"].append({"path": rel, "sha256": digest, "kind": "ci", "added": utc_now(),
                                "reason": "CGSpec governance workflow (spec 8 trust root)"})
        write_json(frozen_path, data)


def _wire_one(fd: int, event: str, owns_already: bool) -> tuple[str, bool]:
    """Write one hook through the bound directory fd. Returns (note, claimed): claimed is True
    only when this checkout may now consider the event its own (a fresh slot, or one it already
    owned); an existing foreign-looking hook in an unconfirmed shared directory is never claimed."""
    kind = git_hooks.entry_kind(fd, event)
    if kind == "symlink":
        return f"left symlinked git {event} hook alone", False
    if kind == "other":
        return f"left existing git {event} hook alone; add the CGSpec call to it yourself", False
    expected = git_hooks.expected_hook_bytes(GIT_HOOK, HOOK, event)
    if kind == "file":
        if git_hooks.read_hook(fd, event) != expected:
            return f"left existing git {event} hook alone; add the CGSpec call to it yourself", False
        if not owns_already:
            return (f"left existing git {event} hook alone; shared hooks directory not yet "
                    "confirmed as this checkout's own"), False
        return f"git {event} hook already matches", True
    git_hooks.write_hook(fd, event, expected, 0o755)
    return f"wired git {event} hook", True


def _wire_git(root: Path, record: dict) -> list[str]:
    hooks, reason, needs_owner_record = git_hooks.directory(root)
    if hooks is None:
        msg = f"{reason}; add the CGSpec call yourself" if "add the CGSpec call" not in reason else reason
        return [msg]
    owns_already = not needs_owner_record or git_hooks.check_ownership(root, hooks, record) is None
    try:
        hooks.mkdir(parents=True, exist_ok=True)
        fd = git_hooks.open_hooks_dir(hooks)
    except (OSError, CgspecError) as exc:
        return [f"cannot prepare git hooks directory ({exc}): add the CGSpec call yourself"]
    notes, claimed_any = [], False
    try:
        for event in git_hooks.EVENTS:
            note, claimed = _wire_one(fd, event, owns_already)
            notes.append(note)
            if claimed:
                claimed_any = True
                if event not in record.get("git_hooks", []):
                    record.setdefault("git_hooks", []).append(event)
    finally:
        os.close(fd)
    if needs_owner_record and claimed_any:
        git_hooks.bind_ownership(root, hooks, record)
    return notes


def wire(root: Path, tools: list[str], owned: dict, profile: str = "") -> list[str]:
    notes, record = [], owned.setdefault("adapters", {})
    if "claude" in tools:
        created, status_wired, previous, user_chain, note = claude_statusline.wire(
            root / ".claude/settings.json", _claude)
        record["claude"] = {"path": ".claude/settings.json", "created": created,
                            "status_line_wired": status_wired, "status_line_previous": previous,
                            "status_line_user_chain": user_chain}
        launcher = shutil.which("claude")
        record["claude"]["launcher"] = str(Path(launcher).absolute()) if launcher else None
        notes.append("wired .claude/settings.json")
        notes.append(note)
    if "antigravity" in tools:
        record["antigravity"] = {"path": ".agents/hooks.json", "created": _merge_json(root / ".agents/hooks.json", _antigravity)}
        notes.append("wired .agents/hooks.json (Antigravity sends no user message to hooks: inbox capture unavailable there)")
        from .agent_skills import install as install_skills
        skills = install_skills(root, Path(__file__).resolve().parents[2])
        if skills:
            record["antigravity"]["skills"] = skills
            notes.append(f"copied {sum(1 for f in skills if f.endswith('SKILL.md'))} CGSpec skills into .agents/skills/")
    if "codex" in tools:
        record["codex"] = {"path": ".codex/hooks.json", "created": _merge_json(root / ".codex/hooks.json", _codex)}
        notes.append("wired .codex/hooks.json")
    if "opencode" in tools:
        record["opencode"] = {"path": ".opencode/hooks.json", "created": _merge_json(root / ".opencode/hooks.json", _opencode)}
        notes.append("wired .opencode/hooks.json")
    if "git" in tools:
        notes += _wire_git(root, record)
    if "ci" in tools:
        rel, select = ".github/workflows/cgspec.yml", ".github/cgspec-select.sh"
        visual_job = VISUAL_WORKFLOW.rstrip() if profile in {"website", "web-workspace", "mobile", "small"} else ""
        workflow = WORKFLOW.format().replace("      # CGSPEC_VISUAL_JOB", visual_job)
        ok, note = _write_new(root, rel, workflow)
        ok2, note2 = _write_new(root, select, SELECT, 0o755)
        notes += [note, note2]
        if ok and ok2:
            record["ci"] = rel
            record["ci_select"] = select
            _freeze_workflow(root, rel)
            _freeze_workflow(root, select)
            notes.append(GITHUB_STEPS)
    if "cursor" in tools:
        ok, note = _write_new(root, ".cursor/rules/cgspec.mdc", CURSOR_RULE)
        notes.append(note)
        if ok:
            record["cursor"] = ".cursor/rules/cgspec.mdc"
    return notes


def refresh(root: Path, owned: dict) -> list[str]:
    """Refresh installed AI-host adapters without changing unrelated user hooks."""
    record, notes = owned.get("adapters", {}), []
    notes += git_hooks.migrate(root, record, GIT_HOOK, HOOK)
    if git_hooks.owned_names(record):
        notes += _wire_git(root, record)
    if "claude" in record:
        path = root / ".claude/settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {})
            for event in list(hooks):
                hooks[event] = [entry for entry in hooks[event] if MARK not in json.dumps(entry)]
                if not hooks[event]:
                    del hooks[event]
            _claude(data)
            status_note = claude_statusline.refresh(data, record["claude"])
            write_json(path, data)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise CgspecError(f"cannot refresh Claude hooks: {exc}") from exc
        notes.append("refreshed Claude hooks")
        notes.append(status_note)
        launcher = shutil.which("claude")
        record["claude"]["launcher"] = str(Path(launcher).absolute()) if launcher else None
    if "antigravity" in record:
        path = root / ".agents/hooks.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "cgspec" not in data:
                raise ValueError("CGSpec hook block is missing")
        except (OSError, ValueError) as exc:
            raise CgspecError(f"cannot refresh Antigravity hooks: {exc}") from exc
        from .agent_skills import refresh as refresh_skills
        files, backups = refresh_skills(root, Path(__file__).resolve().parents[2],
                                        record["antigravity"].get("skills", []))
        _antigravity(data)
        write_json(path, data)
        record["antigravity"]["skills"] = files
        notes.append(f"refreshed Antigravity hooks and {len(files)} skill files; archived {backups} old copies")
    if "codex" in record:
        path = root / ".codex/hooks.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _strip_hook_entries(data)
            _codex(data)
            write_json(path, data)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise CgspecError(f"cannot refresh Codex hooks: {exc}") from exc
        notes.append("refreshed Codex hooks")
    if "opencode" in record:
        path = root / ".opencode/hooks.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _strip_hook_entries(data)
            _opencode(data)
            write_json(path, data)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise CgspecError(f"cannot refresh OpenCode hooks: {exc}") from exc
        notes.append("refreshed OpenCode hooks")
    return notes


def _strip_claude(path: Path, record: dict) -> None:
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if MARK not in json.dumps(e)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    claude_statusline.remove(data, record)
    if record.get("created", False) and not data:
        path.unlink()
    else:
        write_json(path, data)


def _strip_tool(path: Path, created: bool) -> None:
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return
    _strip_hook_entries(data)
    if created and not data:
        path.unlink()
    else:
        write_json(path, data)


def unwire(root: Path, owned: dict) -> list[str]:
    record = owned.get("adapters", {})
    notes = git_hooks.remove(root, record, GIT_HOOK, HOOK)
    if "claude" in record:
        _strip_claude(root / ".claude/settings.json", record["claude"])
    if "antigravity" in record:
        path = root / ".agents/hooks.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("cgspec", None)
            if data or not record["antigravity"].get("created"):
                write_json(path, data)
            else:
                path.unlink()
        from .agent_skills import uninstall as uninstall_skills
        uninstall_skills(root, record["antigravity"].get("skills", []))
    if "codex" in record:
        _strip_tool(root / ".codex/hooks.json", record["codex"].get("created", False))
    if "opencode" in record:
        _strip_tool(root / ".opencode/hooks.json", record["opencode"].get("created", False))
    paths = [root / record[k] for k in ("ci", "ci_select", "cursor") if k in record]
    for path in paths:
        if path.is_file() and not path.is_symlink() and (MARK in path.read_text(errors="replace") or "CGSpec" in path.read_text(errors="replace")):
            os.chmod(path, 0o644)
            path.unlink()
    for rel in (".claude", ".agents", ".codex", ".opencode", ".cursor/rules", ".cursor", ".github/workflows", ".github"):
        try:
            (root / rel).rmdir()
        except OSError:
            pass
    return notes
