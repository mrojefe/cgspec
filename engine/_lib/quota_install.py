"""Installer support for the volatile local quota snapshot."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .common import CgspecError, sha256_file

QUOTA_FILE = "docs/_cgspec/07-state/quota.json"
GITIGNORE_LINES = [".cgspec-scratch/", "docs/.memlog.lock", QUOTA_FILE, "__pycache__/", "*.pyc"]


def prepare(root: Path, owned: dict, safe_target) -> str | None:
    gitignore = safe_target(root, ".gitignore")
    text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    added = [line for line in GITIGNORE_LINES if line not in text.splitlines()]
    if added:
        if text and not text.endswith("\n"):
            text += "\n"
        if not gitignore.exists():
            owned["created"][".gitignore"] = ""
        gitignore.write_text(text + "\n".join(added) + "\n", encoding="utf-8")
        owned["gitignore_added"] = sorted(set(owned.get("gitignore_added", [])) | set(added))
        if ".gitignore" in owned["created"]:
            owned["created"][".gitignore"] = sha256_file(gitignore)
    return untrack(root, QUOTA_FILE)


def untrack(root: Path, quota_file: str) -> str | None:
    """Keep the snapshot on disk while removing a legacy tracked copy from the index."""
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", quota_file], cwd=root,
                             capture_output=True, text=True)
    if tracked.returncode:
        return None
    removed = subprocess.run(["git", "rm", "--cached", "-f", "--ignore-unmatch", "--", quota_file], cwd=root,
                             capture_output=True, text=True)
    if removed.returncode:
        raise CgspecError(f"could not untrack volatile {quota_file}: {removed.stderr.strip()}")
    return f"untracked volatile {quota_file}; the local reading was preserved"
