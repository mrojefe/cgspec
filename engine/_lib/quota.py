"""Interpret quota snapshots conservatively; never collect or infer provider usage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .common import STATE_DIR
from .schema import validate_named

MAX_AGE_SECONDS = 300
CHECKPOINT_REMAINING_PCT = 15
CONTEXT = "conversation context capacity: unavailable from provider quota; use a separate host signal"


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp needs a timezone")
    return parsed.astimezone(timezone.utc)


def _window(data: dict | None, now: datetime) -> tuple[float | None, str]:
    if not isinstance(data, dict) or "used_pct" not in data:
        return None, "window unavailable"
    used = data["used_pct"]
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None, "invalid usage"
    if not 0 <= used <= 100:
        return None, "invalid usage"
    try:
        if "resets_at" in data and _time(data["resets_at"]) <= now:
            return None, "window reset; fresh reading required"
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None, "invalid reset time"
    return 100 - used, ""


def _age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def evaluate(data, *, now: datetime | None = None) -> dict:
    """Return safety advice, not a provider-enforced task gate or quota measurement."""
    result = {"mode": "bounded", "reason": "no valid quota reading available",
              "remaining_pct": None, "windows": {}, "weekly_resume": "unknown", "context": CONTEXT}
    if not isinstance(data, dict):
        return result
    metadata = {key: value for key, value in data.items() if key not in ("five_hour", "weekly")}
    if validate_named(metadata, "quota"):
        return result
    if data["source"] == "unavailable":
        return result
    now = now or datetime.now(timezone.utc)
    try:
        age = (now - _time(data["read_at"])).total_seconds()
        if age < 0 or age > MAX_AGE_SECONDS:
            result["reason"] = ("future quota timestamp" if age < 0 else
                                f"stale quota reading ({_age_text(age)} old)")
            return result
    except (ValueError, TypeError, AttributeError, OverflowError):
        result["reason"] = "invalid quota timestamp or window"
        return result
    windows = [_window(data.get(key), now) for key in ("five_hour", "weekly")]
    known = [value for value, reason in windows if value is not None]
    issues = [reason for value, reason in windows if reason]
    remaining = min(known) if known else None
    result.update(source=data["source"], read_at=data["read_at"], remaining_pct=remaining)
    result["windows"] = dict(zip(("five_hour", "weekly"), (v for v, _ in windows)))
    weekly = result["windows"]["weekly"]
    if weekly is not None:
        result["weekly_resume"] = "blocked" if weekly <= 10 else "quota-permits"
    if remaining is not None and remaining <= 5:
        result.update(mode="emergency", reason="save logs and checkpoint; no new code or subtasks")
    elif remaining is not None and remaining <= CHECKPOINT_REMAINING_PCT:
        result.update(mode="checkpoint", reason="finish the atomic unit, checkpoint and pause")
    elif issues:
        result["reason"] = "; ".join(issues)
    elif remaining is not None and remaining <= 25:
        result.update(mode="frugal", reason="core acceptance work only; no optional research or refactors")
    else:
        result.update(mode="normal", reason="fresh quota snapshot; recheck before expensive work")
    return result


def snapshot(root: Path) -> dict:
    try:
        data = json.loads((root / STATE_DIR / "quota.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    return evaluate(data)


def summary(result: dict) -> str:
    lines = [f"quota: {result['mode']} — {result['reason']}"]
    if result.get("read_at"):
        values = ", ".join(f"{key} {value:g}% remaining" for key, value in result["windows"].items()
                           if value is not None)
        lines.append(f"{values or 'no usable window'} (source {result['source']}, read {result['read_at']})")
    if result["weekly_resume"] == "blocked":
        lines.append("weekly quota: no automatic wake-up")
    lines.append(result["context"])
    return "\n".join(lines)
