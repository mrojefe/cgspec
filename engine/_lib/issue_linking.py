"""Resolve issue references and verify them against the current GitHub repository."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .gitview import git

ISSUE_PATTERN = re.compile(r"(?i)(?:#(\d+)\b|\bgh-(\d+)\b|\b(?:closes|closed|fixes|fixed|resolves|resolved|issue)\s+(\d+)\b)")
ISSUE_URL_PATTERN = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)\b", re.I)
QUALIFIED_ISSUE_PATTERN = re.compile(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)\b")
BRANCH_ISSUE_PATTERN = re.compile(
    r"(?i)(?:^|[/-])(?:(?:issue|gh)[-/]?|#|(?:fix|fixes|feat|feature)[-/]#?)(\d+)(?:[/-]|$)|"
    r"^(?:[a-zA-Z0-9_-]+/)?(\d+)(?:[/-]|$)"
)


def repository(root: Path) -> str | None:
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if os.environ.get("GITHUB_REPOSITORY") and (not workspace or Path(workspace).resolve() == root.resolve()):
        return os.environ["GITHUB_REPOSITORY"]
    remote = git(root, "config", "--get", "remote.origin.url").stdout.strip()
    match = re.search(r"(?:github\.com[:/])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", remote)
    return match.group(1) if match else None


def _references(text: str, repo: str) -> set[tuple[str, int]]:
    found: set[tuple[str, int]] = set()
    covered: list[tuple[int, int]] = []
    for pattern in (ISSUE_URL_PATTERN, QUALIFIED_ISSUE_PATTERN):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in covered):
                continue
            covered.append(match.span())
            found.add((f"{match.group(1)}/{match.group(2)}", int(match.group(3))))
    for match in ISSUE_PATTERN.finditer(text):
        if any(start <= match.start() < end for start, end in covered):
            continue
        if re.search(r"(?i)\b(?:PR|pull request)\s*$", text[max(0, match.start() - 16):match.start()]):
            continue
        found.add((repo, int(next(group for group in match.groups() if group is not None))))
    return {(owner, number) for owner, number in found if number > 0}


def references(root: Path, commits: list[str], repo: str, pr: str | None = None) -> set[tuple[str, int]]:
    found = _references(pr or "", repo)
    for commit in commits:
        msg = git(root, "log", "-1", "--format=%B", commit).stdout
        found.update(_references(msg, repo))
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not branch or branch == "HEAD":
        branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or ""
    if branch and branch not in ("main", "master"):
        found.update((repo, int(match.group(1) or match.group(2))) for match in BRANCH_ISSUE_PATTERN.finditer(branch))
    return found


def event_pr_metadata() -> str:
    """Read the actual Actions pull-request payload supplied to the installed workflow."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        raise RuntimeError("GitHub pull_request event payload is unavailable")
    try:
        event = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("GitHub pull_request event payload is unreadable") from exc
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pr, dict) or not isinstance(pr.get("title"), str) or not isinstance(pr.get("body"), (str, type(None))):
        raise RuntimeError("GitHub pull_request event payload has no PR metadata")
    return "\n".join((pr["title"], pr.get("body") or ""))


def existing_issue(target_repo: str, referenced_repo: str, number: int, credential: str) -> bool:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    target = _github_json(f"{api}/repos/{target_repo}", credential)
    if not isinstance(target, dict) or not isinstance(target.get("id"), int):
        raise RuntimeError("GitHub repository lookup returned an invalid identity")
    issue = _github_json(f"{api}/repos/{referenced_repo}/issues/{number}", credential)
    if issue is None:
        return False
    if not isinstance(issue, dict) or issue.get("number") != number:
        raise RuntimeError("GitHub issue lookup returned an invalid response")
    canonical = issue.get("repository_url")
    if not isinstance(canonical, str):
        raise RuntimeError("GitHub issue lookup omitted the repository identity")
    issue_repo = canonical.rstrip("/").rsplit("/repos/", 1)
    if len(issue_repo) != 2:
        raise RuntimeError("GitHub issue lookup returned an invalid repository identity")
    referenced = _github_json(f"{api}/repos/{issue_repo[1]}", credential)
    if not isinstance(referenced, dict) or not isinstance(referenced.get("id"), int):
        raise RuntimeError("GitHub repository lookup returned an invalid identity")
    return target["id"] == referenced["id"] and "pull_request" not in issue


def _github_json(url: str, credential: str) -> dict | None:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {credential}", "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"GitHub issue lookup returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RuntimeError(f"GitHub issue lookup unavailable: {type(exc).__name__}") from exc
