"""session-start pack, session-check and the stop gate (spec 8 lifecycle, spec 9 resume)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .checkpoint_cmd import CMD
from .common import MEMLOG_FILE, STATE_DIR
from .recovery_pack import context
from .stop_state import MAX_STOP_BLOCKS, RETRIES, stop_decision
from .quota import snapshot, summary

LEDGER_TIME = re.compile(r"^### \[[^\]]+\] (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", re.M)


def _load(root: Path, rel) -> dict:
    try:
        data = json.loads((root / rel).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_ledger(root: Path, count: int = 5) -> list[str]:
    path = root / MEMLOG_FILE
    if not path.is_file():
        return []
    heads = [l for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l.startswith("### [")]
    return heads[-count:]


def pack(root: Path, doctor_summary: str, tool: str = "") -> str:
    cp, recovery = context(root)
    tier = _load(root, STATE_DIR / "tier.json").get("active")
    lines = ["CGSpec session pack (read docs/_cgspec/01-engine/CONTRACT.md before acting)",
             f"goal: {cp.get('goal') or '(not set)'}",
             "stages: " + (", ".join(f"{k}: {v}" for k, v in cp.get("stages", {}).items()) or "(none yet)"),
             f"next action: {cp.get('next_action') or '(none)'}",
             f"in flight: {cp.get('in_flight') or 'nothing'}",
             f"active tier: {tier['tier'] + ' ' + tier['id'] + ' - ' + tier['reason'] if tier else 'none'}"]
    if isinstance(cp.get("awaiting_user"), dict):
        asked = cp["awaiting_user"]
        # `used` means a stop accepted the question, not that it was answered; hosts without a stop check
        # never set it. So the pack never claims the user still owes an answer.
        label = "last question asked" if asked.get("used") else "question asked, check the conversation for the answer"
        count = asked.get("count", 1)
        extra = f"; {count} questions asked during this batch" if isinstance(count, int) and count > 1 else ""
        lines.append(f"{label} ({asked.get('at', '?')}{extra}): {asked.get('question', '')}")
    if cp.get("open_issues"):
        lines.append("open issues: " + "; ".join(cp["open_issues"]))
    lines += recovery
    quota_result = snapshot(root)
    lines.append(summary(quota_result))
    if tool == "claude":
        from .quota_notice import claude_supervision
        lines.append(claude_supervision(root, quota_result))
    lines.append("recent ledger:")
    lines += [f"  {l}" for l in last_ledger(root)] or ["  (empty)"]
    lines.append(f"doctor: {doctor_summary}")
    return "\n".join(lines)


def stop_problems(root: Path, doctor_errors: int) -> list[str]:
    problems = []
    if doctor_errors:
        problems.append(f"doctor reports {doctor_errors} errors; see them with python3 docs/_cgspec/01-engine/doctor.py, "
                        "fix them, or record why you cannot with: " + CMD + " --blocked '<why>'")
    from .checkpoint_cmd import load
    from .common import CgspecError
    try:
        cp = load(root)
    except CgspecError:
        cp = {}  # doctor reports corruption; preserve it, keep the bounded stop protocol usable
    if cp.get("in_flight") and not _unused_question(cp):
        problems.append(f"work still in flight: {cp['in_flight']}; finish it and run {CMD} --done, "
                        f"or run {CMD} --blocked '<why>', or if you are stopping to ask the user, "
                        f"run {CMD} --awaiting '<your question>'")
    path = root / MEMLOG_FILE
    if path.is_file():
        times = LEDGER_TIME.findall(path.read_text(encoding="utf-8", errors="replace"))
        if times and cp.get("updated", "") < max(times):
            problems.append(f"checkpoint is older than the latest ledger entry; run {CMD} --next '<what comes next>'")
    return problems


def _unused_question(cp: dict) -> bool:
    asked = cp.get("awaiting_user")
    return isinstance(asked, dict) and asked.get("used") is False and bool(str(asked.get("question", "")).strip())


def mark_question_used(root: Path) -> None:
    """A recorded question excuses exactly one stop; afterwards in-flight work is checked again.

    The question itself stays, so whoever resumes sees what the user was asked. The semantic
    `updated` time is left alone, as for the other mechanical stop records.
    """
    from .checkpoint_cmd import load, save
    from .common import CgspecError
    try:
        cp = load(root)
        if _unused_question(cp):
            cp["awaiting_user"]["used"] = True
            save(root, cp)
    except CgspecError:
        pass  # doctor reports a damaged checkpoint; never turn that into a stop failure here


def capture_before_compact(root: Path) -> None:
    import sys
    from .checkpoint_cmd import capture_only
    from .common import CgspecError
    try:
        capture_only(root)
    except CgspecError as exc:
        print(f"CGSpec pre-compact capture failed: {exc}", file=sys.stderr)
