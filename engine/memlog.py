#!/usr/bin/env python3
"""memlog.py: the only writer of docs/_memlog.md, the append-only project ledger.

Commands:
  add     append an entry with a collision-free ID (M-YYYYMMDD-xxxx)
  check   report duplicate IDs and malformed headers (exit 1 if any)
  list    print entries, optionally filtered by tag
  export  write entries after a given ID to a file (used by adoption rollback)
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True  # never leave caches in the vendored engine
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.common import (  # noqa: E402
    EXIT_FAIL, EXIT_INTERNAL, EXIT_OK, MEMLOG_FILE, CgspecError, new_id, resolve_root, utc_now,
)

HEADER = "# Project Memory Log (_memlog.md)\n> Append-only. Written only by memlog.py.\n"
STATUSES = ["Draft", "Proposed", "Approved", "Delegated", "Rejected", "Superseded", "Imported"]
HEADER_LINE = re.compile(r"^### \[([^\]]*)\](.*)$", re.M)
FULL_HEADER = re.compile(
    r"^ (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| tags: ([^|]+)\| owner: ([^|]+)\| perms: (\S.*)$"
)
VALID_ID = re.compile(r"^(?:[a-z0-9-]+:)?M-(?:\d{8}-[0-9a-f]{4}|\d{3,})$")


@dataclass
class Entry:
    id: str
    line: int
    header_ok: bool
    tags: list[str]
    text: str


def parse(text: str) -> list[Entry]:
    matches = list(HEADER_LINE.finditer(text))
    entries = []

    current_line = 1
    current_pos = 0

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        fields = FULL_HEADER.match(match.group(2))

        # Performance optimization: Count lines incrementally rather than from the
        # beginning of the file to avoid an O(N^2) time complexity bottleneck.
        current_line += text.count("\n", current_pos, start)
        current_pos = start

        entries.append(Entry(
            id=match.group(1).strip(),
            line=current_line,
            header_ok=bool(fields) and bool(VALID_ID.match(match.group(1).strip())),
            tags=[t.strip() for t in fields.group(2).split(",") if t.strip()] if fields else [],
            text=text[start:end].rstrip("\n") + "\n",
        ))
    return entries


class Lock:
    """OS-level exclusive lock on a side file; released automatically if the writer dies."""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path, self.timeout, self.handle = path, timeout, None

    def _try(self) -> bool:
        busy = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, errno.EDEADLK}
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in busy:
                return False
            self.handle.close()
            raise CgspecError(f"this file system does not support locking ({exc}); move the project to a local disk") from None

    def __enter__(self) -> "Lock":
        self.handle = open(self.path, "a+")
        deadline = time.monotonic() + self.timeout
        while not self._try():
            if time.monotonic() >= deadline:
                self.handle.close()
                raise TimeoutError(f"ledger is locked by another writer ({self.path})")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        if os.name == "nt":
            import msvcrt
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()  # closing releases the lock; the side file stays, harmlessly


FORGED_LINE = re.compile(r"^\s*(###\s*\[|[-*]\s*\*\*)")
PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def clean_field(name: str, value: str) -> str:
    if any(ch in value for ch in "\n\r|"):
        raise CgspecError(f"--{name} must be a single line without '|'")
    if not value.strip():
        raise CgspecError(f"--{name} must not be empty")
    return value.strip()


def clean_details(value: str | None) -> str | None:
    """Multi-line details are allowed but indented, and may not imitate headers or fields."""
    if not value:
        return None
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line in lines[1:]:
        if FORGED_LINE.match(line):
            raise CgspecError("--details may not contain lines that look like ledger headers or fields")
    if FORGED_LINE.match(lines[0]):
        raise CgspecError("--details may not start like a ledger header or field")
    return "\n  ".join(line.rstrip() for line in lines)


def cmd_add(root: Path, args: argparse.Namespace) -> int:
    tags = clean_field("tags", args.tags)
    owner = clean_field("owner", args.owner)
    perms = clean_field("perms", args.perms)
    summary = clean_field("summary", args.summary)
    details = clean_details(args.details)
    links = clean_field("links", args.links) if args.links else None
    if args.repo_prefix and not PREFIX.match(args.repo_prefix):
        raise CgspecError("--repo-prefix must match ^[a-z0-9][a-z0-9-]*$")
    approval = clean_field("approval-ref", args.approval_ref) if args.approval_ref else None
    if approval:
        from _lib.inbox import approval_exists
        if approval != "trailer" and not approval_exists(root, approval):
            raise CgspecError(f"approval {approval} is not in 07-state/approvals.json (only your own message creates one)")
        if args.status == "Approved" and approval != "trailer":
            target = f"decision:{' '.join(summary.split()).casefold()}"
            if not approval_exists(root, approval, target):
                raise CgspecError(f"approval {approval} does not name decision: {summary}")
    if args.status == "Approved" and not approval:
        # Spec 8: approvals come only from the user's message or commit trailer; the hook that
        # records them passes the reference, and CI verifies it exists.
        raise CgspecError("status Approved needs --approval-ref (inbox entry or commit with your trailer)")

    log = root / MEMLOG_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    with Lock(log.parent / ".memlog.lock", args.lock_timeout):
        existing = log.read_text(encoding="utf-8") if log.exists() else ""
        taken = {e.id for e in parse(existing)}
        prefix = f"{args.repo_prefix}:" if args.repo_prefix else ""
        entry_id = prefix + new_id("M")
        while entry_id in taken:
            entry_id = prefix + new_id("M")

        lines = [f"### [{entry_id}] {utc_now()} | tags: {tags} | owner: {owner} | perms: {perms}",
                 f"- **Summary:** {summary}"]
        if details:
            lines.append(f"- **Details:** {details}")
        if args.status:
            lines.append(f"- **Status:** {args.status}")
        if approval:
            lines.append(f"- **Approval:** {approval}")
        if links:
            lines.append(f"- **Links:** {links}")
        block = "\n".join(lines) + "\n"

        if not existing:
            existing_prefix = HEADER + "\n"
        else:
            existing_prefix = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        # Append in place: keeps inode, permissions and owner of the file.
        with log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(existing_prefix + block)
            handle.flush()
            os.fsync(handle.fileno())
    print(entry_id)
    return EXIT_OK


def cmd_check(root: Path, _args: argparse.Namespace) -> int:
    log = root / MEMLOG_FILE
    if not log.exists():
        print(f"missing ledger: {MEMLOG_FILE}")
        return EXIT_FAIL
    entries = parse(log.read_text(encoding="utf-8"))
    problems = []
    seen: dict[str, int] = {}
    for entry in entries:
        if not entry.header_ok:
            problems.append(f"line {entry.line}: malformed header for [{entry.id}]")
        if entry.id in seen:
            problems.append(f"line {entry.line}: duplicate id {entry.id} (first at line {seen[entry.id]})")
        else:
            seen[entry.id] = entry.line
    for problem in problems:
        print(problem)
    print(f"{len(entries)} entries, {len(problems)} problems")
    return EXIT_FAIL if problems else EXIT_OK


def cmd_list(root: Path, args: argparse.Namespace) -> int:
    log = root / MEMLOG_FILE
    entries = parse(log.read_text(encoding="utf-8")) if log.exists() else []
    for entry in entries:
        if args.tag and args.tag not in entry.tags:
            continue
        print(entry.text.splitlines()[0])
    return EXIT_OK


def cmd_export(root: Path, args: argparse.Namespace) -> int:
    log = root / MEMLOG_FILE
    entries = parse(log.read_text(encoding="utf-8")) if log.exists() else []
    ids = [e.id for e in entries]
    if args.since not in ids:
        raise CgspecError(f"id not found in ledger: {args.since}")
    after = entries[ids.index(args.since) + 1:]
    Path(args.out).write_text("".join(e.text + "\n" for e in after), encoding="utf-8")
    print(f"exported {len(after)} entries to {args.out}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memlog.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", help="project root (default: nearest folder with docs/_cgspec/project.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("--summary", required=True)
    add.add_argument("--tags", required=True, help="comma-separated")
    add.add_argument("--details")
    add.add_argument("--status", choices=STATUSES)
    add.add_argument("--links", help="comma-separated IDs, e.g. ADR-004,SLICE-012")
    add.add_argument("--owner", default=os.environ.get("CGSPEC_OWNER", "@agent"))
    add.add_argument("--perms", default="public")
    add.add_argument("--repo-prefix", default=os.environ.get("CGSPEC_REPO_PREFIX", ""))
    add.add_argument("--approval-ref", help="required with --status Approved")
    add.add_argument("--lock-timeout", type=float, default=10.0)

    sub.add_parser("check")
    lst = sub.add_parser("list")
    lst.add_argument("--tag")
    exp = sub.add_parser("export")
    exp.add_argument("--since", required=True)
    exp.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"add": cmd_add, "check": cmd_check, "list": cmd_list, "export": cmd_export}
    try:
        root = resolve_root(args.root)
        return handlers[args.command](root, args)
    except CgspecError as exc:
        print(f"memlog: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except (TimeoutError, PermissionError) as exc:
        print(f"memlog: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
