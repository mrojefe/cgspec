"""Install, refresh and remove the Claude quota status-line wrapper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex

from .common import write_json

STATUS = "docs/_cgspec/01-engine/quota_statusline.py"


def command(chain: str | None = None, *, chain_user: bool = False) -> str:
    base = '"${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"/'
    value = f"python3 {base}{STATUS}"
    if chain:
        value += f" --chain {shlex.quote(chain)}"
    if chain_user:
        value += " --chain-user-statusline"
    return value


def _user_statusline() -> object:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    folder = Path(configured).expanduser() if configured else Path.home() / ".claude"
    try:
        data = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
        return data.get("statusLine") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def add(data: dict) -> tuple[bool, dict | None, bool, str]:
    existing = data.get("statusLine")
    if existing is None:
        user = _user_statusline()
        if user is not None:
            if (isinstance(user, dict) and user.get("type") == "command" and
                    isinstance(user.get("command"), str) and user["command"].strip()):
                replacement = json.loads(json.dumps(user))
                replacement["command"] = command(chain_user=True)
                replacement.pop("refreshInterval", None)
                data["statusLine"] = replacement
                return (True, None, True,
                        "wired Claude statusLine quota collector and dynamically chained the user-scope command")
            return False, None, False, "left existing user-scope Claude statusLine alone; add the CGSpec collector manually"
        data["statusLine"] = {"type": "command", "command": command()}
        return True, None, False, "wired Claude statusLine quota collector (event-only)"
    if isinstance(existing, dict) and STATUS in str(existing.get("command", "")):
        return True, None, "--chain-user-statusline" in existing.get("command", ""), "Claude statusLine quota collector already wired"
    if (isinstance(existing, dict) and existing.get("type") == "command" and
            isinstance(existing.get("command"), str) and existing["command"].strip()):
        previous = json.loads(json.dumps(existing))
        replacement = json.loads(json.dumps(existing))
        replacement["command"] = command(existing["command"])
        replacement.pop("refreshInterval", None)
        data["statusLine"] = replacement
        return True, previous, False, "wired Claude statusLine quota collector and chained the existing command"
    return False, None, False, "left existing Claude statusLine alone; add the CGSpec collector manually"


def wire(path: Path, add_hooks) -> tuple[bool, bool, dict | None, bool, str]:
    data, created = {}, not path.exists()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return created, False, None, False, "left invalid .claude/settings.json alone"
    if not isinstance(data, dict):
        return created, False, None, False, "left non-object .claude/settings.json alone"
    add_hooks(data)
    wired, previous, user_chain, note = add(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    return created, wired, previous, user_chain, note


def refresh(data: dict, record: dict) -> str:
    current = data.get("statusLine")
    if isinstance(current, dict) and STATUS in str(current.get("command", "")):
        previous = record.get("status_line_previous")
        replacement = json.loads(json.dumps(current))
        replacement["command"] = command(previous.get("command") if isinstance(previous, dict) else None,
                                         chain_user=bool(record.get("status_line_user_chain")))
        replacement.pop("refreshInterval", None)
        data["statusLine"] = replacement
        record["status_line_wired"] = True
        return "refreshed Claude statusLine quota collector"
    if "status_line_wired" not in record:
        wired, previous, user_chain, note = add(data)
        record.update(status_line_wired=wired, status_line_previous=previous,
                      status_line_user_chain=user_chain)
        return note
    return "left changed Claude statusLine alone; CGSpec quota collector is not active"


def remove(data: dict, record: dict) -> None:
    status = data.get("statusLine")
    if not isinstance(status, dict) or STATUS not in str(status.get("command", "")):
        return
    previous = record.get("status_line_previous")
    if isinstance(previous, dict):
        data["statusLine"] = previous
    else:
        data.pop("statusLine", None)
