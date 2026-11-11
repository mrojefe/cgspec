"""Tool payload normalization and content projection for AI assistants."""

from __future__ import annotations

import json
from pathlib import Path
import re

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
AG_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
AG_PATH_KEYS = {"TargetFile", "AbsolutePath", "Cwd"}
CODEX_WRITE_TOOLS = {"apply_patch", "file_edit", "write_file", "str_replace", "write", "edit"}
CODEX_SHELL_TOOLS = {"bash", "Bash", "exec_command", "shell", "terminal"}
OPENCODE_WRITE_TOOLS = {"edit", "write", "file_edit", "patch", "create_file"}
OPENCODE_SHELL_TOOLS = {"bash", "shell", "command", "exec"}


def _strings(value, skip=frozenset()) -> list[str]:
    # Perf: closure-based traversal avoids ~2x overhead from intermediate lists
    # and recursive unpacks during deep JSON payload parsing.
    result: list[str] = []
    def _visit(v):
        if isinstance(v, str):
            result.append(v)
        elif isinstance(v, dict):
            for k, val in v.items():
                if k not in skip:
                    _visit(val)
        elif isinstance(v, list):
            for val in v:
                _visit(val)
    _visit(value)
    return result


def _parse_json(data):
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def _normalize_claude(payload: dict) -> dict:
    name, data = payload.get("tool_name", ""), payload.get("tool_input") or {}
    if name in WRITE_TOOLS:
        path = data.get("file_path") or data.get("notebook_path")
        edits = data.get("edits") or []
        content = data.get("content") or data.get("new_string") or data.get("new_source") or \
            "\n".join(e.get("new_string", "") for e in edits if isinstance(e, dict))
        return {"kind": "write", "paths": [path] if path else [], "content": content,
                "new": name == "Write" and None, "cwd": payload.get("cwd")}
    if name == "Bash":
        return {"kind": "shell", "command": data.get("command", ""), "cwd": payload.get("cwd")}
    return {"kind": "other"}


def _normalize_antigravity(payload: dict) -> dict:
    call = payload.get("toolCall") or {}
    name, data = call.get("name", ""), call.get("args") or {}
    cwd = (payload.get("workspacePaths") or [None])[0]
    if name in AG_WRITE_TOOLS:
        return {"kind": "write", "paths": [data["TargetFile"]] if data.get("TargetFile") else [],
                "content": "\n".join(_strings(data, AG_PATH_KEYS)), "cwd": cwd}
    if name == "run_command":
        return {"kind": "shell", "command": data.get("CommandLine", ""), "cwd": data.get("Cwd") or cwd}
    return {"kind": "other"}


def _normalize_codex(payload: dict) -> dict:
    name = payload.get("tool_name") or payload.get("name") or payload.get("tool") or ""
    raw = payload.get("tool_input") or payload.get("input") or payload.get("args") or payload.get("parameters") or {}
    data = _parse_json(raw)
    cwd = payload.get("cwd") or data.get("cwd")
    if name in CODEX_WRITE_TOOLS:
        path = data.get("file_path") or data.get("path") or data.get("target_file") or data.get("file")
        if not path and "patch" in data and isinstance(data["patch"], str):
            m = re.search(r"(?:---|\+\+\+|diff --git a/)\s*([^\s\t\n]+)", data["patch"])
            if m:
                p = m.group(1).removeprefix("a/").removeprefix("b/")
                if p != "/dev/null":
                    path = p
        content = data.get("content") or data.get("new_str") or data.get("new_string") or data.get("patch") or ""
        return {"kind": "write", "paths": [path] if path else [], "content": content, "cwd": cwd}
    if name in CODEX_SHELL_TOOLS:
        cmd = data.get("command") or data.get("cmd") or data.get("CommandLine") or ""
        return {"kind": "shell", "command": cmd, "cwd": cwd}
    return {"kind": "other"}


def _normalize_opencode(payload: dict) -> dict:
    name = payload.get("tool") or payload.get("action") or payload.get("tool_name") or payload.get("name") or ""
    raw = payload.get("args") or payload.get("input") or payload.get("parameters") or payload.get("tool_input") or {}
    data = _parse_json(raw)
    cwd = payload.get("cwd") or payload.get("directory") or data.get("cwd")
    if name in OPENCODE_WRITE_TOOLS:
        path = data.get("filePath") or data.get("file_path") or data.get("path") or data.get("file")
        content = data.get("content") or data.get("text") or data.get("new_string") or data.get("diff") or data.get("patch") or ""
        return {"kind": "write", "paths": [path] if path else [], "content": content, "cwd": cwd}
    if name in OPENCODE_SHELL_TOOLS:
        cmd = data.get("command") or data.get("cmd") or ""
        return {"kind": "shell", "command": cmd, "cwd": cwd}
    return {"kind": "other"}


def _normalize_generic(payload: dict) -> dict:
    name = (payload.get("tool") or payload.get("tool_name") or payload.get("action") or payload.get("name") or "")
    raw = (payload.get("tool_input") or payload.get("args") or payload.get("input") or payload.get("parameters") or {})
    data = _parse_json(raw)
    cwd = payload.get("cwd") or data.get("cwd")
    if name in {"write", "edit", "apply_patch", "file_edit", "write_file", "str_replace", "patch"}:
        path = (payload.get("paths") or [data.get("file_path") or data.get("filePath") or
                                         data.get("path") or data.get("file")] if not payload.get("paths") else payload.get("paths"))
        paths = [p for p in path if p]
        content = payload.get("content") or data.get("content") or data.get("new_string") or data.get("new_str") or data.get("patch") or ""
        return {"kind": "write", "paths": paths, "content": content, "cwd": cwd}
    if name in {"shell", "bash", "Bash", "exec_command", "run_command"}:
        cmd = payload.get("command") or (data.get("command") or data.get("cmd") or data.get("CommandLine") if isinstance(data, dict) else "")
        return {"kind": "shell", "command": cmd or "", "cwd": cwd}
    if payload.get("paths") or payload.get("content"):
        return {"kind": "write", "paths": payload.get("paths", []), "content": payload.get("content"), "cwd": cwd}
    if payload.get("command"):
        return {"kind": "shell", "command": payload.get("command", ""), "cwd": cwd}
    return {"kind": "other"}


def normalize(payload: dict, tool: str) -> dict:
    """Turn a tool-specific payload into {kind: write|shell|other, paths, content, command, cwd}."""
    if tool == "claude":
        return _normalize_claude(payload)
    if tool == "antigravity":
        return _normalize_antigravity(payload)
    if tool == "codex":
        return _normalize_codex(payload)
    if tool == "opencode":
        return _normalize_opencode(payload)
    return _normalize_generic(payload)


def _apply_patch(orig: str, patch_text: str) -> str | None:
    """Best-effort unified diff application for content projection."""
    lines = orig.splitlines(keepends=True)
    patch_lines = patch_text.splitlines()
    i = 0
    while i < len(patch_lines):
        line = patch_lines[i]
        m = re.match(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@", line)
        if not m:
            i += 1
            continue
        old_start = max(0, int(m.group(1)) - 1)
        i += 1
        hunk_old, hunk_new = [], []
        while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
            pl = patch_lines[i]
            if pl.startswith("+"):
                hunk_new.append(pl[1:])
            elif pl.startswith("-"):
                hunk_old.append(pl[1:])
            elif pl.startswith(" "):
                hunk_new.append(pl[1:])
                hunk_old.append(pl[1:])
            i += 1
        target_sub = "".join(l.rstrip("\r\n") + "\n" for l in lines[old_start:old_start + len(hunk_old)])
        old_sub = "".join(l.rstrip("\r\n") + "\n" for l in hunk_old)
        if target_sub == old_sub or not hunk_old:
            new_lines = [l + "\n" for l in hunk_new]
            lines[old_start:old_start + len(hunk_old)] = new_lines
        else:
            return None
    return "".join(lines)


def _project_antigravity(target: Path, payload: dict, fallback: str | None) -> str | None:
    args = (payload.get("toolCall") or {}).get("args") or {}
    name = (payload.get("toolCall") or {}).get("name", "")
    if name in ("replace_file_content", "multi_replace_file_content") and target.is_file():
        chunks = args.get("ReplacementChunks") or [
            {"TargetContent": args.get("TargetContent", ""), "ReplacementContent": args.get("ReplacementContent", "")}
        ]
        try:
            orig = target.read_text(encoding="utf-8", errors="replace")
            replaced = orig
            for c in chunks:
                t, r = c.get("TargetContent", ""), c.get("ReplacementContent", "")
                if t and t in replaced:
                    replaced = replaced.replace(t, r, 1)
            if replaced != orig:
                return replaced
        except OSError:
            return fallback
    elif name == "write_to_file":
        if args.get("Append") and target.is_file():
            try:
                return target.read_text(encoding="utf-8", errors="replace") + (args.get("CodeContent") or "")
            except OSError:
                return fallback
        if args.get("CodeContent") is not None:
            return args.get("CodeContent")
    return fallback


def _project_claude(target: Path, payload: dict, fallback: str | None) -> str | None:
    name, data = payload.get("tool_name", ""), payload.get("tool_input") or {}
    if name in ("Edit", "MultiEdit") and target.is_file():
        edits = data.get("edits") or [{"old_string": data.get("old_string", ""), "new_string": data.get("new_string", "")}]
        try:
            orig = target.read_text(encoding="utf-8", errors="replace")
            replaced = orig
            for e in edits:
                o, n = e.get("old_string", ""), e.get("new_string", "")
                if o and o in replaced:
                    replaced = replaced.replace(o, n, 1)
            if replaced != orig:
                return replaced
        except OSError:
            return fallback
    if name == "Write" and data.get("content") is not None:
        return data.get("content")
    return fallback


def _project_codex_opencode(target: Path, payload: dict, fallback: str | None) -> str | None:
    raw = (payload.get("tool_input") or payload.get("args") or payload.get("input") or
           payload.get("parameters") or payload)
    data = _parse_json(raw)
    content = data.get("content") if data.get("content") is not None else (
        data.get("text") if data.get("text") is not None else data.get("CodeContent")
    )
    if content is not None:
        return content
    old_s = data.get("old_str") or data.get("old_string") or data.get("old_text")
    new_s = data.get("new_str") or data.get("new_string") or data.get("new_text") or ""
    if old_s and target.is_file():
        try:
            orig = target.read_text(encoding="utf-8", errors="replace")
            if old_s in orig:
                return orig.replace(old_s, new_s, 1)
        except OSError:
            return fallback
    patch_text = data.get("patch") or data.get("diff")
    if patch_text and target.is_file():
        try:
            orig = target.read_text(encoding="utf-8", errors="replace")
            patched = _apply_patch(orig, patch_text)
            if patched is not None:
                return patched
        except Exception:
            return fallback
    return fallback


def project_content(root: Path, rel: str, payload: dict, tool: str, fallback: str | None) -> str | None:
    """Project the resulting file content after a tool edit or write."""
    target = root / rel
    if tool == "antigravity":
        return _project_antigravity(target, payload, fallback)
    if tool == "claude":
        return _project_claude(target, payload, fallback)
    if tool in ("codex", "opencode", "generic"):
        return _project_codex_opencode(target, payload, fallback)
    return fallback
