"""Event-driven quota mode notices for the principal Claude agent."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .common import atomic_write_text
from .quota import snapshot

REPEAT_SECONDS = 300


def _cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".cache"


def _state_path(root: Path, session_id: str, cache_root: Path | None) -> Path:
    key = hashlib.sha256(f"{root.resolve()}\0{session_id}".encode()).hexdigest()
    return (cache_root or _cache_root()) / "cgspec/quota-notices" / f"{key}.json"


def _message(result: dict, previous: str | None) -> str:
    mode = result["mode"]
    if mode == "bounded":
        return ("CGSpec quota supervision unavailable: " + result["reason"] +
                ". Continue only in bounded, resumable work; this advisory repeats after five minutes while unavailable.")
    if mode == "emergency":
        return ("CGSpec quota emergency: save logs and checkpoint, start no new work, and stabilize or pause the "
                "current atomic unit. This advisory repeats after five minutes until recovery.")
    if previous in {"bounded", "checkpoint", "emergency", "frugal"} and mode == "normal":
        return "CGSpec quota supervision recovered: the fresh provider reading is in normal mode."
    return f"CGSpec quota mode changed to {mode}: {result['reason']}."


def _claude_result(root: Path) -> dict:
    result = snapshot(root)
    if result.get("source") != "claude-statusline":
        result = dict(result)
        result.update(mode="bounded", reason="no fresh Claude statusLine provider reading")
    return result


def _collector_configured(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    command = value.get("command")
    return (value.get("type") == "command" and isinstance(command, str) and
            "quota_statusline.py" in command)


def next_notice(root: Path, session_id: str, *, cache_root: Path | None = None,
                monotonic_now: float | None = None) -> str | None:
    """Return a notice only on transitions or five-minute bounded/emergency reminders."""
    try:
        result = _claude_result(root)
        mode = result["mode"]
        now = time.monotonic() if monotonic_now is None else monotonic_now
        path = _state_path(root, session_id or "unknown", cache_root)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            previous = state.get("mode") if isinstance(state, dict) else None
            last = state.get("monotonic") if isinstance(state, dict) else None
        except (OSError, ValueError):
            previous, last = None, None
        repeated = mode in {"bounded", "emergency"}
        elapsed = now - last if isinstance(last, (int, float)) else None
        due = previous != mode or (repeated and (elapsed is None or elapsed < 0 or elapsed >= REPEAT_SECONDS))
        if not due:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"mode": mode, "monotonic": now}) + "\n")
        return _message(result, previous)
    except Exception:
        return None


def claude_supervision(root: Path, result: dict | None = None) -> str:
    """Describe startup health without claiming that settings prove a provider reading."""
    configured = False
    try:
        settings = json.loads((root / ".claude/settings.json").read_text(encoding="utf-8"))
        configured = (isinstance(settings, dict) and
                      _collector_configured(settings.get("statusLine")))
    except (OSError, ValueError):
        pass
    try:
        local = json.loads((root / ".claude/settings.local.json").read_text(encoding="utf-8"))
        if isinstance(local, dict) and "statusLine" in local:
            configured = _collector_configured(local["statusLine"])
    except (OSError, ValueError):
        pass
    current = result or snapshot(root)
    if configured and current.get("source") == "claude-statusline" and current["mode"] != "bounded":
        return "quota supervision: healthy — Claude statusLine collector configured with a fresh provider reading"
    if not configured:
        return ("quota supervision warning: Claude statusLine collector is not configured; "
                "the session continues in bounded mode")
    return ("quota supervision warning: Claude statusLine collector is configured but no fresh complete provider "
            "reading is available; the session continues in bounded mode")
