#!/usr/bin/env python3
"""Consume Claude Code statusLine JSON and atomically refresh the local quota snapshot.

This command is host-driven and event-only. It never fetches quota, never estimates usage,
and never turns a status-line failure into a Claude Code failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.common import STATE_DIR, atomic_write_text  # noqa: E402


def _cache_root() -> Path:
    configured = os.environ.get("XDG_CACHE_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".cache"


def _key(root: Path, session_id: str) -> str:
    return hashlib.sha256(f"{root.resolve()}\0{session_id}".encode()).hexdigest()


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        handle.close()


def _number(value, *, percentage: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("not a finite number")
    if percentage and not 0 <= value <= 100:
        raise ValueError("percentage outside 0..100")
    return value


def _iso_epoch(value) -> str:
    epoch = _number(value)
    parsed = datetime.fromtimestamp(epoch, timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(value: object, now: datetime) -> dict:
    if not isinstance(value, dict):
        raise ValueError("window is not an object")
    used = _number(value.get("used_percentage"), percentage=True)
    reset = value.get("resets_at")
    reset_iso = _iso_epoch(reset)
    if datetime.fromtimestamp(_number(reset), timezone.utc) <= now:
        raise ValueError("window has reset")
    return {"used_pct": used, "resets_at": reset_iso}


def _reading(payload: object, now: datetime) -> tuple[str, int, dict]:
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    session_id = payload.get("session_id")
    cache = payload.get("prompt_cache")
    limits = payload.get("rate_limits")
    if not isinstance(session_id, str) or not session_id or not isinstance(cache, dict) or not isinstance(limits, dict):
        raise ValueError("required host fields are absent")
    requests = cache.get("requests")
    if isinstance(requests, bool) or not isinstance(requests, int) or requests < 1:
        raise ValueError("prompt_cache.requests is invalid")
    reading = {
        "schema": 1,
        "read_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "claude-statusline",
    }
    mapped = (("five_hour", "five_hour"), ("seven_day", "weekly"))
    for source, target in mapped:
        if source in limits:
            try:
                reading[target] = _window(limits[source], now)
            except (ValueError, OverflowError, OSError):
                continue
    if not any(name in reading for name in ("five_hour", "weekly")):
        raise ValueError("no usable quota window")
    return session_id, requests, reading


def update(root: Path, payload: object, *, cache_root: Path | None = None,
           now: datetime | None = None) -> bool:
    """Write one proven-new response. Invalid or repeated host data is a no-op."""
    try:
        root = root.resolve()
        now = now or datetime.now(timezone.utc)
        session_id, requests, reading = _reading(payload, now)
        state_dir = (cache_root or _cache_root()) / "cgspec/quota"
        key = _key(root, session_id)
        state = state_dir / f"{key}.json"
        with _locked(state_dir / f"{key}.lock"):
            try:
                previous = json.loads(state.read_text(encoding="utf-8"))
                previous_requests = previous.get("requests", -1) if isinstance(previous, dict) else -1
            except (OSError, ValueError):
                previous_requests = -1
            if isinstance(previous_requests, int) and requests <= previous_requests:
                return False
            atomic_write_text(root / STATE_DIR / "quota.json", json.dumps(reading, indent=2) + "\n")
            atomic_write_text(state, json.dumps({"requests": requests}) + "\n")
        return True
    except Exception:
        return False


def _run_chain(command: str | None, raw: str) -> None:
    if not command:
        return
    try:
        result = subprocess.run(command, shell=True, input=raw, text=True, capture_output=True, timeout=10)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
    except Exception:
        pass


def _user_chain() -> str | None:
    try:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        folder = Path(configured).expanduser() if configured else Path.home() / ".claude"
        data = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
        status = data.get("statusLine") if isinstance(data, dict) else None
        command = status.get("command") if isinstance(status, dict) and status.get("type") == "command" else None
        if isinstance(command, str) and command.strip() and "docs/_cgspec/01-engine/quota_statusline.py" not in command:
            return command
    except (OSError, ValueError):
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root")
    parser.add_argument("--chain")
    parser.add_argument("--chain-user-statusline", action="store_true")
    try:
        args, _ = parser.parse_known_args(argv)
        root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[3]
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        update(root, payload)
        _run_chain(_user_chain() if args.chain_user_statusline else args.chain, raw)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
