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


def _eval_model(model_id: str, data: dict, now: datetime) -> dict:
    if not isinstance(data, dict) or "used_pct" not in data:
        return {"model": model_id, "status": "unknown", "remaining_pct": None}
    used = data["used_pct"]
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        return {"model": model_id, "status": "invalid", "remaining_pct": None}
    remaining = 100 - used
    status = data.get("status")
    if not status:
        if remaining <= 2:
            status = "exhausted"
        elif remaining <= 20:
            status = "warning"
        else:
            status = "nominal"
    resets_at = data.get("resets_at")
    return {
        "model": model_id,
        "remaining_pct": remaining,
        "status": status,
        "resets_at": resets_at,
    }


def evaluate(data, *, now: datetime | None = None) -> dict:
    """Return safety advice, not a provider-enforced task gate or quota measurement."""
    result = {"mode": "bounded", "reason": "no valid quota reading available",
              "remaining_pct": None, "windows": {}, "models": {}, "default_fallback": {},
              "weekly_resume": "unknown", "context": CONTEXT}
    if not isinstance(data, dict):
        return result
    metadata = {key: value for key, value in data.items()
                if key not in ("five_hour", "weekly", "models", "default_fallback")}
    if validate_named(metadata, "quota"):
        return result
    if data["source"] == "unavailable":
        return result
    now = now or datetime.now(timezone.utc)
    try:
        age = (now - _time(data["read_at"])).total_seconds()
        if age < 0 or age > MAX_AGE_SECONDS:
            result["reason"] = "future quota timestamp" if age < 0 else "stale quota reading"
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

    # Evaluate per-model matrix if present
    if isinstance(data.get("models"), dict):
        result["models"] = {
            m_id: _eval_model(m_id, m_data, now)
            for m_id, m_data in data["models"].items()
        }
    if isinstance(data.get("default_fallback"), dict):
        result["default_fallback"] = dict(data["default_fallback"])

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


def evaluate_subagent_preflight(data, model_name: str | None = None, *, now: datetime | None = None) -> dict:
    """Evaluate pre-flight eligibility before invoking or waking a subagent."""
    evaluated = evaluate(data, now=now)
    models = evaluated.get("models", {})
    fallbacks = evaluated.get("default_fallback", {})

    # If general emergency stop is active, subagents cannot be invoked
    if evaluated["mode"] == "emergency":
        return {
            "allowed": False,
            "decision": "deny",
            "reason": f"Global quota emergency stop active ({evaluated['remaining_pct']}% remaining).",
            "fallback_model": None,
        }

    # If specific model requested or inherited
    target = model_name or "default"
    model_info = models.get(target)

    # Check alias or generic match (e.g. gemini-argon, gemini-4-argon)
    if not model_info:
        for k, v in models.items():
            if target in k or k in target:
                model_info = v
                target = k
                break

    if model_info and model_info.get("status") == "exhausted":
        fallback = fallbacks.get(target)
        return {
            "allowed": False,
            "decision": "deny",
            "reason": f"Model tier '{target}' is exhausted (0% remaining; resets at {model_info.get('resets_at', 'unknown')}).",
            "fallback_model": fallback,
        }

    return {"allowed": True, "decision": "allow", "reason": "Quota nominal.", "fallback_model": None}


def evaluate_assistant_prompt(data, *, now: datetime | None = None) -> dict:
    """Pre-flight check for the interactive conversational assistant on user prompt submit."""
    evaluated = evaluate(data, now=now)
    models = evaluated.get("models", {})
    exhausted_models = [m for m, v in models.items() if v.get("status") == "exhausted"]

    if evaluated["mode"] == "emergency":
        return {
            "status": "emergency",
            "warning": f"Attention: Quota critique ({evaluated['remaining_pct']}% restant). Sauvegarde en cours.",
            "exhausted_models": exhausted_models,
        }
    if exhausted_models:
        return {
            "status": "warning",
            "warning": f"Attention: Les modèles suivants sont épuisés: {', '.join(exhausted_models)}.",
            "exhausted_models": exhausted_models,
        }
    return {"status": "nominal", "warning": None, "exhausted_models": []}


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
    if result.get("models"):
        m_lines = [f"{m}: {v.get('remaining_pct')}% ({v.get('status')})" for m, v in result["models"].items()]
        lines.append(f"models: {', '.join(m_lines)}")
    if result["weekly_resume"] == "blocked":
        lines.append("weekly quota: no automatic wake-up")
    lines.append(result["context"])
    return "\n".join(lines)
