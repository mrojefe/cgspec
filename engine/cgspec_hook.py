#!/usr/bin/env python3
"""CGSpec tool adapter entry point; see FORMATS.md for payloads and exit codes."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never leave caches in the vendored engine
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import hook_heartbeat, tiers  # noqa: E402
from _lib.common import EXIT_BLOCK, EXIT_FAIL, EXIT_INTERNAL, EXIT_OK, CgspecError, resolve_root  # noqa: E402
from _lib.gates import Context, check_write, project_content  # noqa: E402
from _lib.shellparse import write_targets  # noqa: E402
from _lib.tool_payloads import normalize  # noqa: E402

CLI_MODULES = {"start": "start", "run": "runs", "git-plan": "git_protocol", "proof": "proof_commands", "adopt": "discover", "close": "closure", "release": "release", "capability": "capabilities"}


def shell_reasons(ctx: Context, command: str, cwd: str | None) -> list[str]:
    from _lib.gates import credential_read_reasons, git_tracked, protected_command_reasons, protected_list
    from _lib.shellparse import WRITE_HINTS, destructive_git_reasons, mentions
    import posixpath
    targets, deletes, inline, broad = write_targets(command, cwd or str(ctx.root))
    guarded = protected_list(ctx)
    reasons = credential_read_reasons(command) + destructive_git_reasons(command) + protected_command_reasons(command)
    if re.search(r"\binstall\.py\b[^;&|]*\b(uninstall|upgrade|install)\b", command):
        reasons.append("installing, upgrading or uninstalling CGSpec is done by the user, not the agent; "
                       "tell the user the exact install.py command to run")
    for raw in targets:
        rel = ctx.relative(raw)
        if rel is not None and rel != ".":
            reasons += check_write(ctx, rel, None)
    for raw in deletes:
        rel = ctx.relative(raw)
        if rel is None or rel == ".":
            continue
        if ctx.protected(rel) or any(p.startswith(rel.rstrip("/") + "/") for p in guarded):
            reasons.append(f"deleting {rel} would remove protected files")
        elif (ctx.root / rel).exists() and git_tracked(ctx.root, rel):
            reasons += check_write(ctx, rel, None, is_new=False)
    import fnmatch
    for item in broad:
        raw, _, pattern_text = item.partition("\0")
        patterns = [p for p in pattern_text.split("\0") if p]
        rel = ctx.relative(raw)
        inside = [p for p in guarded if rel is not None and (rel == "." or p.startswith(rel.rstrip("/") + "/") or p == rel)]
        if patterns:  # find -name/-path: only the protected files the patterns can match
            files = []
            for p in inside:
                folder = ctx.root / p
                files += ([f.relative_to(ctx.root).as_posix() for f in folder.rglob("*") if f.is_file()]
                          if p.endswith("/") and folder.is_dir() else [p])
            inside = [p for p in files if any(fnmatch.fnmatch(posixpath.basename(p), pat) or fnmatch.fnmatch(p, pat)
                                              for pat in patterns)]
        if inside:
            reasons.append(f"this command may change everything under {rel}, including protected files; use the edit tools instead")
    for code in inline:
        if WRITE_HINTS.search(code):
            reasons += [f"inline script writes to protected {p}" for p in guarded
                        if p in code or (not p.endswith("/") and posixpath.basename(p) in code)]
    reasons += [f"command names protected {p} and looks like a write" for p in mentions(command, guarded)]
    return reasons


def pre_tool(root: Path, tool: str, raw: str) -> int:
    ctx, touched = None, []
    try:
        ctx = Context.load(root)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
        tool_name = payload.get("tool_name") or payload.get("tool") or ""
        if "invoke_subagent" in tool_name:
            from _lib import quota
            params = payload.get("parameters") or {}
            pre = quota.evaluate_subagent_preflight(quota.snapshot(root), params.get("Model"))
            if not pre.get("allowed", True):
                fb = f" (fallback: {pre['fallback_model']})" if pre.get("fallback_model") else ""
                return block(tool, f"CGSpec Quota Pre-flight: {pre.get('reason')}{fb}")
        call = normalize(payload, tool)
        from _lib import quota
        q_snap = quota.snapshot(root)
        if q_snap.get("mode") == "emergency" and call.get("kind") in ("write", "shell"):
            return block(tool, f"CGSpec Quota Emergency Stop active ({q_snap.get('remaining_pct')}% remaining)")
        # The witness key is outside the checkout. Ordinary edit/shell tools must
        # not manufacture receipts or inspect the witness to forge their MACs.
        coverage_paths = ("hook-heartbeats", "cgspec-hook-witness-")
        if any(marker in p for p in call.get("paths", []) for marker in coverage_paths) or \
                any(marker in call.get("command", "") for marker in coverage_paths):
            return block(tool, "CGSpec hook coverage evidence is engine-owned; do not read or write its witness or receipts")
        if call["kind"] == "other":
            if tool == "antigravity": print('{"decision":"allow"}')
            return EXIT_OK
        if call["kind"] == "shell":
            reasons = shell_reasons(ctx, call["command"], call.get("cwd"))
        else:
            reasons = []
            for raw_path in call["paths"]:
                rel = ctx.relative(raw_path, call.get("cwd"))
                if rel is not None:
                    touched.append(rel)
                    content = project_content(root, rel, payload, tool, call.get("content"))
                    reasons += check_write(ctx, rel, content)
                else:
                    if os.environ.get("CGSPEC_UNIT") or os.environ.get("CGSPEC_RUN"):
                        reasons.append(f"{raw_path} is outside project root or escapes via symlink")
    except Exception as exc:  # fail closed only where it matters
        if ctx and any(ctx.protected(rel) for rel in touched):
            return block(tool, f"CGSpec blocked (internal error on a protected path): {exc!r}")
        print(f"CGSpec hook internal error (not blocking; commit hook and CI still check): {exc!r}", file=sys.stderr)
        if tool == "antigravity": print('{"decision":"allow"}')
        return EXIT_OK if tool == "antigravity" else EXIT_INTERNAL
    if reasons:
        return block(tool, "Blocked by CGSpec:\n- " + "\n- ".join(dict.fromkeys(reasons)))
    if tool == "antigravity": print('{"decision":"allow"}')
    return EXIT_OK


def block(tool: str, message: str) -> int:
    """Claude Code reads exit 2 + stderr; Antigravity reads a JSON decision on stdout."""
    if tool == "antigravity":
        print(json.dumps({"decision": "deny", "reason": message}))
        return EXIT_OK
    print(message, file=sys.stderr)
    return EXIT_BLOCK


def staged_doctor(root: Path, doctor, staged: list[str] | None = None) -> tuple[list, list[str]]:
    """Run the doctor and the registered checks on exactly what is staged, not on the working tree."""
    import shutil
    import subprocess
    import tempfile
    from _lib import checks
    tmp = Path(tempfile.mkdtemp(prefix="cgspec-index-"))
    try:
        subprocess.run(["git", "checkout-index", "-a", "-f", f"--prefix={tmp}/"], cwd=root, check=True,
                       capture_output=True)
        # Candidate = the staged snapshot in tmp; base = HEAD of the real repository.
        failures, _ = checks.run(tmp, staged or [], write_evidence=False, base_rev="HEAD", repo_root=root)
        return doctor.run_checks(tmp), failures
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pre_commit(root: Path) -> int:
    coverage = hook_heartbeat.problems(root)
    if coverage:
        print(hook_heartbeat.report(coverage), file=sys.stderr)
        return EXIT_FAIL
    from _lib.commit_gate import evaluate
    from _lib.gitview import View
    import doctor
    view = View.staged(root)
    reasons = evaluate(root, view)
    staged = [c.path for c in view.changes() if c.status != "D"]
    findings, check_failures = staged_doctor(root, doctor, staged)
    reasons += [f"doctor {f.code} {f.path}: {f.message}" for f in findings if f.is_error]
    reasons += [f"check {failure}" for failure in check_failures]
    if reasons:
        print("CGSpec commit check failed:\n- " + "\n- ".join(reasons))
        return EXIT_FAIL
    print("CGSpec commit check passed")
    return EXIT_OK


def ci(root: Path, base: str | None, rerun: bool, args: argparse.Namespace) -> int:
    from _lib import ci_gate
    import doctor
    problems, notes = ci_gate.run(root, base, rerun, Path(args.release_dir) if args.release_dir else None)
    problems += [f"doctor {f.code} {f.path}: {f.message}" for f in doctor.run_checks(root) if f.is_error]
    for note in notes:
        print(note)
    if problems:
        print("CGSpec governance failed:\n- " + "\n- ".join(problems))
        return EXIT_FAIL
    print("CGSpec governance passed")
    return EXIT_OK


def doctor_state(root: Path) -> tuple[int, str]:
    import doctor
    findings = doctor.run_checks(root)
    errors = [f for f in findings if f.is_error]
    summary = f"{len(errors)} errors, {len(findings) - len(errors)} warnings"
    if errors:
        summary += " - " + "; ".join(f"{f.code} {f.path}" for f in errors[:5])
    return len(errors), summary


def lifecycle(root: Path, event: str, args: argparse.Namespace, raw: str = "") -> int:
    from _lib import inbox, session
    if event == "user-message":
        try:
            inbox.record(root, tool=args.tool, raw=raw)
            from _lib import quota
            adv = quota.evaluate_assistant_prompt(quota.snapshot(root))
            if adv.get("warning"):
                print(f"CGSpec Advisory: {adv['warning']}", file=sys.stderr)
        except Exception as exc:  # never block the user's message
            print(f"cgspec_hook: inbox capture failed: {exc!r}", file=sys.stderr)
        return EXIT_OK
    errors, summary = doctor_state(root)
    if event in ("session-start", "pre-compact"):
        if event == "pre-compact":
            session.capture_before_compact(root)
        pack = session.pack(root, summary)
        print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": pack}}]})
              if args.tool == "antigravity" else pack)
        return EXIT_OK
    if event == "session-check":
        return hook_heartbeat.session_check(root, args.tool, errors, summary)
    if event == "stop":
        from _lib.doc_sync import sync
        try:
            sync(root)  # keep visible document dates true before judging the end of the turn
        except Exception as exc:
            print(f"cgspec_hook: document date sync failed: {exc!r}", file=sys.stderr)
        errors, summary = doctor_state(root)
        allow, message = session.stop_decision(root, session.stop_problems(root, errors))
        if allow:
            session.mark_question_used(root)
        if args.tool == "antigravity":
            if not allow:
                print(json.dumps({"decision": "continue", "reason": message}))
            else:
                print('{"decision":"allow"}')
                if message: print(message, file=sys.stderr)
            return EXIT_OK
        if message:
            print(message, file=sys.stderr)
        return EXIT_OK if allow else EXIT_BLOCK
    if event == "commit-msg":
        from _lib.gates import EMOJI
        if not args.rest:
            raise CgspecError("commit-msg needs the message file path")
        text = Path(args.rest[0]).read_text(encoding="utf-8", errors="replace")
        if EMOJI.search(text):
            print("CGSpec: decorative emoji in the commit message (spec 1)")
            return EXIT_FAIL
        from _lib import commit_trust
        problem = commit_trust.commit_msg_problem(root, text)
        if problem:
            print(problem)
            return EXIT_FAIL
        return EXIT_OK
    raise CgspecError(f"unknown event: {event}")


def post_tool(root: Path, tool: str, raw: str) -> int:
    """Never blocks the finished tool; exit 2 only to show the agent a budget warning."""
    from _lib.post_tool import HELPER_TOOLS, count_helper, mark_stale
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            if tool == "antigravity": print("{}")
            return EXIT_OK
        ctx = Context.load(root)
        call, changed = normalize(payload, tool), []
        if call["kind"] == "write":
            changed = [ctx.relative(p, call.get("cwd")) for p in call["paths"]]
        elif call["kind"] == "shell":
            targets, deletes, _, _ = write_targets(call["command"], call.get("cwd") or str(root))
            changed = [ctx.relative(p) for p in targets + deletes]
        mark_stale(root, [c for c in changed if c])
        warning = count_helper(root) if payload.get("tool_name") in HELPER_TOOLS else None
    except Exception as exc:
        print(f"cgspec_hook: post-tool failed (not blocking): {exc!r}", file=sys.stderr)
        if tool == "antigravity": print("{}")
        return EXIT_OK
    if warning:
        print(warning, file=sys.stderr)
        if tool == "antigravity": print("{}")
        return EXIT_BLOCK if tool == "claude" else EXIT_OK
    if tool == "antigravity": print("{}")
    return EXIT_OK


def cmd_doc(root: Path, args: argparse.Namespace) -> int:
    from _lib.doc_sync import sync
    if (args.rest[:1] or [""])[0] != "sync":
        raise CgspecError("usage: cgspec_hook.py doc sync")
    changed = sync(root)
    print("\n".join(f"dated {rel}" for rel in changed) or "document dates already up to date")
    return EXIT_OK


def cmd_capture_approval(root: Path, args: argparse.Namespace) -> int:
    from _lib import inbox
    text = args.message or args.prompt or (" ".join(args.rest) if args.rest else None)
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        raise CgspecError("capture-approval requires approval text: pass as argument, --message, or stdin")
    inbox_id, approval_id = inbox.capture(root, text, tool=args.tool or "generic")
    if approval_id:
        print(f"captured approval {approval_id} ({inbox_id})")
        return EXIT_OK
    else:
        print(f"recorded in inbox ({inbox_id}), but text did not match an approval pattern", file=sys.stderr)
        return EXIT_FAIL


def cmd_tier(root: Path, args: argparse.Namespace) -> int:
    action = args.rest[0] if args.rest else ""
    if action not in ("set", "show", "clear"):
        raise CgspecError("usage: cgspec_hook.py tier set|show|clear")
    if action == "show":
        print(json.dumps(tiers.active(root), indent=2))
        return EXIT_OK
    if action == "clear":
        tiers.clear(root)
        print("tier cleared")
        return EXIT_OK
    if not args.tier or not args.reason:
        raise CgspecError("tier set needs --tier and --reason")
    ctx = Context.load(root)
    files = [f.strip() for f in (args.files or "").split(",") if f.strip()]
    entry = tiers.set_tier(root, args.tier, args.reason, files, args.slice, ctx.classes, ctx.protected, args.verify)
    from memlog import main as memlog_main
    detail = ", ".join(files) if files else entry.get("slice", "")
    memlog_main(["--root", str(root), "add", "--tags", "tier", "--summary",
                 f"tier {entry['tier']} {entry['id']}: {entry['reason']} ({detail})"])
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cgspec_hook.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("event", help="tier, pre-tool, capture-approval, or a later hook event")
    parser.add_argument("rest", nargs="*", help="tier: set|show|clear; commit-msg: message file")
    parser.add_argument("--root")
    parser.add_argument("--tool", default="generic", choices=["claude", "antigravity", "codex", "opencode", "cursor", "git", "generic"])
    parser.add_argument("--tier", choices=["T0", "T1", "T2", "T3"])
    parser.add_argument("--reason")
    parser.add_argument("--files", help="T0: comma-separated existing files")
    parser.add_argument("--slice", help="T1+: SLICE-NNN")
    parser.add_argument("--base", help="ci: base commit of the range to check")
    from _lib.checkpoint_cmd import add_arguments
    add_arguments(parser)
    for f in ("--file", "--applies-to", "--description", "--approval", "--command", "--guide", "--release-dir", "--repos", "--json-out", "--target", "--slices", "--id", "--message", "--prompt"):
        parser.add_argument(f)
    parser.add_argument("--no-rerun", action="store_true")
    for f in ("--criteria", "--tests", "--proofs", "--outcomes", "--supersession", "--units", "--run-id", "--by", "--status", "--unit", "--unit-status", "--outcome", "--verify", "--commit-kind", "--pr"):
        parser.add_argument(f)
    parser.add_argument("--review-rounds", type=int)
    parser.add_argument("--checks-passed", action="store_true")
    from _lib.proof_commands import add_arguments as add_proof_arguments
    add_proof_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_intermixed_args(argv)
    try:
        root = resolve_root(args.root)
        if args.event in hook_heartbeat.EVENTS:
            return hook_heartbeat.dispatch(root, args, sys.modules[__name__])
        if args.event == "tier": return cmd_tier(root, args)
        if args.event == "capture-approval": return cmd_capture_approval(root, args)
        if args.event in CLI_MODULES: return importlib.import_module(f"_lib.{CLI_MODULES[args.event]}").cli(root, args)
        if args.event == "pre-commit": return pre_commit(root)
        if args.event == "post-commit": from _lib.local_approval_gate import post_commit; return post_commit(root)
        if args.event == "pre-push": from _lib.local_approval_gate import pre_push; return pre_push(root, sys.stdin.read())
        if args.event == "post-tool": return post_tool(root, args.tool, sys.stdin.read())
        if args.event == "doc": return cmd_doc(root, args)
        if args.event == "check": from _lib.checks import cli; return cli(root, args)
        if args.event == "ci": return ci(root, args.base, not args.no_rerun, args)
        if args.event == "checkpoint":
            from _lib.checkpoint_cmd import update
            cp = update(root, args)
            print(f"checkpoint updated: next: {cp['next_action'] or '(none)'}; in flight: {cp['in_flight'] or 'nothing'}")
            return EXIT_OK
        if args.event in ("user-message", "session-start", "session-check", "pre-compact", "stop", "commit-msg"):
            return lifecycle(root, args.event, args)
        raise CgspecError(f"unknown event: {args.event}")
    except CgspecError as exc:
        print(f"cgspec_hook: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
