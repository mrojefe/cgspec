#!/usr/bin/env python3
"""install.py: install, upgrade, verify or uninstall CGSpec in a project.

  install    copy the template and a vendored engine; write the version marker
  upgrade    refresh the vendored engine from this plugin; never touches project records
  verify     compare the vendored engine with its hash manifest
  uninstall  remove what install created, keep anything the user changed

No symlinks are ever created; the engine is copied so a project never breaks when the
plugin moves. Running install twice changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never leave caches in the vendored engine
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.common import (  # noqa: E402
    ENGINE_DIR, EXIT_FAIL, EXIT_OK, PROJECT_FILE, CgspecError, read_json, sha256_file, utc_now, write_json,
)
from _lib.adapters import refresh as refresh_adapters, unwire, wire  # noqa: E402
from _lib.document_dates import stamp_new  # noqa: E402
from _lib.layout import GUI_PROFILES  # noqa: E402
from _lib import quota_install  # noqa: E402

HERE = Path(__file__).resolve().parent
PLUGIN_SOURCE = "https://github.com/BeOjefe/cgspec.git"
OWNED = ENGINE_DIR / "owned-files.json"
MANIFEST = ENGINE_DIR / "manifest.json"
ARCHIVE = Path("docs/_cgspec/_archive/pre-cgspec")
REPLACEABLE = {"AGENTS.md", "CLAUDE.md"}
SKIP_NAMES = {"__pycache__", ".pytest_cache", "owned-files.json", "manifest.json"}

PATH_CLASSES = {
    "schema": ["**/migrations/**", "**/schema.prisma", "**/*.sql", "**/schema.graphql"],
    "api": ["**/api/**", "**/openapi.yaml", "**/openapi.yml", "**/openapi.json", "**/*.proto"],
    "auth": ["**/auth/**", "**/*auth*.*"],
    "deps": ["**/package.json", "**/package-lock.json", "**/pnpm-lock.yaml", "**/yarn.lock",
             "**/pyproject.toml", "**/requirements*.txt", "**/poetry.lock", "**/uv.lock",
             "**/go.mod", "**/go.sum", "**/Cargo.toml", "**/Cargo.lock", "**/Gemfile*"],
    "code": ["src/**", "app/**", "lib/**", "packages/*/src/**", "apps/*/src/**", "services/*/src/**"],
    "tests": ["tests/**", "test/**", "**/*.test.*", "**/*.spec.*", "**/test_*.py", "**/*_test.go", "e2e/**"],
    "docs": ["docs/**", "*.md"],
}


def plugin_root() -> Path:
    root = HERE.parent
    if not (root / "templates").is_dir():
        raise CgspecError("templates not found: run install/upgrade from the CGSpec plugin, not a vendored copy")
    return root


def version() -> str:
    return (HERE / "VERSION").read_text(encoding="utf-8").strip()


def engine_files() -> list[Path]:
    files = []
    for path in sorted(HERE.rglob("*")):
        if any(part in SKIP_NAMES for part in path.relative_to(HERE).parts):
            continue
        if path.is_symlink():
            raise CgspecError(f"symlink in engine source is not allowed: {path}")
        if path.is_file() and path.suffix != ".pyc":
            files.append(path)
    return files


def copy_engine(root: Path) -> dict[str, str]:
    target = root / ENGINE_DIR
    hashes = {}
    for src in engine_files():
        rel = src.relative_to(HERE).as_posix()
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        if src.suffix == ".py" and src.parent == HERE:
            os.chmod(dst, 0o755)
        hashes[rel] = sha256_file(dst)
    stale = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} - set(hashes) - {"manifest.json", "owned-files.json"}
    for rel in stale:
        (target / rel).unlink()
    write_json(root / MANIFEST, {"schema": 1, "cgspec_version": version(), "files": hashes})
    return hashes


def template_files(profile: str, topology: str) -> tuple[dict[str, Path], list[str]]:
    """Files (rel -> source) and folders the templates create for this profile and topology."""
    base = plugin_root() / "templates"
    layers = [base / "base"] + ([base / "gui"] if profile in GUI_PROFILES else [])
    if (base / topology).is_dir():
        layers.append(base / topology)
    files: dict[str, Path] = {}
    dirs: list[str] = []
    for layer in layers:
        for src in sorted(layer.rglob("*")):
            rel = src.relative_to(layer).as_posix()
            if src.is_dir():
                dirs.append(rel)
            else:
                files[rel] = src
    return files, dirs


def safe_target(root: Path, rel: str) -> Path:
    """Resolve rel under root, refusing absolute paths, '..' and any symlink on the way."""
    parts = Path(rel).parts
    if Path(rel).is_absolute() or ".." in parts or not parts:
        raise CgspecError(f"refusing unsafe path: {rel}")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise CgspecError(f"refusing to write through a symlink: {current.relative_to(root)}")
    return current


def place_file(root: Path, rel: str, src: Path, owned: dict) -> None:
    dst = safe_target(root, rel)
    if dst.exists():
        if sha256_file(dst) == sha256_file(src):
            owned["created"].setdefault(rel, sha256_file(dst))  # resumed install
            return
        if rel not in REPLACEABLE:
            return  # never overwrite project files
        archive = safe_target(root, (ARCHIVE / rel).as_posix())
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, archive)
        owned["replaced"][rel] = (ARCHIVE / rel).as_posix()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if rel.startswith("docs/") and rel.endswith(".md") and rel not in {"docs/00-INBOX.md", "docs/_memlog.md"}:
        dst.write_text(stamp_new(src.read_text(encoding="utf-8"), utc_now()), encoding="utf-8")
    else:
        shutil.copyfile(src, dst)
    owned["created"][rel] = sha256_file(dst)


def place_templates(root: Path, profile: str, topology: str, owned: dict) -> None:
    files, dirs = template_files(profile, topology)
    for rel in [*files, *dirs]:
        safe_target(root, rel)  # check everything before writing anything
    for rel in dirs:
        target = root / rel
        if not target.exists():
            target.mkdir(parents=True)
            owned["dirs"].append(rel)
    for rel, src in files.items():
        place_file(root, rel, src, owned)


def new_owned() -> dict:
    return {"schema": 1, "cgspec_version": version(), "installed_at": utc_now(),
            "created": {}, "dirs": [], "replaced": {}, "deleted": {}, "gitignore_added": []}


def cmd_install(root: Path, args: argparse.Namespace) -> int:
    if (root / "docs/_cgspec/01-script").exists() and not (root / PROJECT_FILE).exists():
        raise CgspecError("Earlier CGSpec v0.1 layout found (docs/_cgspec/01-script); use the cgspec-adopt upgrade mode")
    if (root / PROJECT_FILE).exists():
        current = read_json(root / PROJECT_FILE).get("cgspec_version")
        if current == version():
            print(f"already installed (CGSpec {current}); nothing changed")
            return EXIT_OK
        raise CgspecError(f"CGSpec {current} is installed; run 'install.py upgrade' to move to {version()}")
    if args.topology.startswith("polyrepo") and not args.repo_prefix:
        raise CgspecError("polyrepo topologies need --repo-prefix")
    safe_target(root, PROJECT_FILE.as_posix())
    owned = read_json(root / OWNED) if (root / OWNED).is_file() else new_owned()  # resume
    place_templates(root, args.profile, args.topology, owned)
    if not (root / ENGINE_DIR).exists():
        owned["dirs"].append(ENGINE_DIR.as_posix())
    copy_engine(root)
    quota_note = quota_install.prepare(root, owned, safe_target)
    if quota_note:
        print(quota_note)
    tools = sorted(set(args.tools.split(",")) - {""})
    for note in wire(root, tools, owned, args.profile):
        print(note)
    write_json(root / OWNED, owned)
    project = {"schema": 1, "cgspec_version": version(), "profile": args.profile, "topology": args.topology,
               "path_classes": PATH_CLASSES, "tools": tools, "protected_branches": ["main", "master"]}
    if args.repo_prefix:
        project["repo_prefix"] = args.repo_prefix
    project["plugin_source"] = args.plugin_source
    write_json(root / PROJECT_FILE, project)  # written last: its presence means "install finished"
    owned["created"][PROJECT_FILE.as_posix()] = sha256_file(root / PROJECT_FILE)
    write_json(root / OWNED, owned)
    print(f"installed CGSpec {version()} ({args.profile}, {args.topology}) in {root}")
    return EXIT_OK


def verify_engine(root: Path) -> list[str]:
    manifest = read_json(root / MANIFEST)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        return ["engine manifest has no 'files' map"]
    target = root / ENGINE_DIR
    problems = []
    for rel, digest in files.items():
        path = target / rel
        if not path.is_file():
            problems.append(f"missing engine file: {rel}")
        elif path.is_symlink():
            problems.append(f"engine file is a symlink: {rel}")
        elif sha256_file(path) != digest:
            problems.append(f"engine file changed: {rel}")
    listed = set(files) | {"manifest.json", "owned-files.json"}
    for path in target.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.relative_to(target).as_posix() not in listed:
            problems.append(f"unexpected engine file: {path.relative_to(target).as_posix()}")
    return problems


def cmd_verify(root: Path, _args: argparse.Namespace) -> int:
    problems = verify_engine(root)
    for problem in problems:
        print(problem)
    print("engine OK" if not problems else f"{len(problems)} engine problems")
    return EXIT_FAIL if problems else EXIT_OK


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))
def cmd_upgrade(root: Path, args: argparse.Namespace) -> int:
    if args.from_v1:
        from _lib.upgrade_v1 import run as upgrade_v1
        code, report = upgrade_v1(root, vars(args), args.apply, args.rollback)
        print(json.dumps(report, indent=2))
        return code
    if not (root / PROJECT_FILE).exists():
        raise CgspecError("CGSpec is not installed here; run 'install.py install'")
    project = read_json(root / PROJECT_FILE)
    old = project.get("cgspec_version", "0.0.0")
    if str(old).startswith("1.") or (root / "docs/_cgspec/01-script").exists():
        raise CgspecError("Earlier CGSpec v0.1 layout needs 'upgrade --from-v1' with a reviewed dry-run")
    if version_tuple(version()) < version_tuple(old) and not args.force:
        raise CgspecError(f"refusing downgrade {old} -> {version()} (use --force if you mean it)")
    owned = read_json(root / OWNED) if (root / OWNED).is_file() else new_owned()
    files, dirs = template_files(project["profile"], project["topology"])
    for rel in dirs:
        if not safe_target(root, rel).exists():
            (root / rel).mkdir(parents=True)
            owned["dirs"].append(rel)
    for rel, src in files.items():
        if not safe_target(root, rel).exists():  # add what a newer template introduced; never overwrite
            place_file(root, rel, src, owned)
    copy_engine(root)
    for note in refresh_adapters(root, owned):
        print(note)
    quota_note = quota_install.prepare(root, owned, safe_target)
    if quota_note:
        print(quota_note)
    owned["cgspec_version"] = version()
    write_json(root / OWNED, owned)
    project["cgspec_version"] = version()  # the version marker is updated last (spec 5)
    project.setdefault("protected_branches", ["main", "master"])
    write_json(root / PROJECT_FILE, project)
    print(f"engine refreshed; CGSpec {old} -> {version()}")
    return EXIT_OK


def remove_created(root: Path, owned: dict, allowed: set[str]) -> list[str]:
    report = []
    for rel, digest in owned.get("created", {}).items():
        if rel not in allowed:
            report.append(f"ignored (not created by install): {rel}")
            continue
        if rel in owned.get("replaced", {}):
            continue
        try:
            path = safe_target(root, rel)
        except CgspecError as exc:
            report.append(f"ignored ({exc})")
            continue
        if not path.is_file():
            continue
        if rel == PROJECT_FILE.as_posix() or sha256_file(path) == digest:
            path.unlink()
        else:
            report.append(f"kept (changed since install): {rel}")
    return report


def restore_replaced(root: Path, owned: dict) -> tuple[list[str], bool]:
    report, archive_needed = [], False
    for rel, archive_rel in owned.get("replaced", {}).items():
        if rel not in REPLACEABLE:
            report.append(f"ignored (not replaceable): {rel}")
            continue
        current, archive = safe_target(root, rel), safe_target(root, archive_rel)
        if current.is_file() and sha256_file(current) != owned["created"].get(rel):
            report.append(f"kept (changed since install): {rel}; original stays in {archive_rel}")
            archive_needed = True
        elif not archive.is_file():
            report.append(f"archive missing for {rel}; left the current file in place")
        else:
            shutil.copy2(archive, current)
    return report, archive_needed


def cmd_uninstall(root: Path, _args: argparse.Namespace) -> int:
    owned = read_json(root / OWNED)
    project = read_json(root / PROJECT_FILE) if (root / PROJECT_FILE).is_file() else {}
    files, dirs = template_files(project.get("profile", "web-workspace"), project.get("topology", "single"))
    allowed = set(files) | {PROJECT_FILE.as_posix(), ".gitignore"}
    report = unwire(root, owned)
    report += remove_created(root, owned, allowed)
    restored, archive_needed = restore_replaced(root, owned)
    report += restored
    gitignore = root / ".gitignore"
    if gitignore.is_file() and not gitignore.is_symlink() and owned.get("gitignore_added"):
        remaining = [l for l in gitignore.read_text(encoding="utf-8").splitlines() if l not in owned["gitignore_added"]]
        if remaining:
            gitignore.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        else:
            gitignore.unlink()
    shutil.rmtree(root / ENGINE_DIR, ignore_errors=True)
    lock = root / "docs/.memlog.lock"
    if lock.is_file() and not lock.is_symlink():
        lock.unlink()
    if not archive_needed:
        shutil.rmtree(root / ARCHIVE, ignore_errors=True)
    remove_empty_dirs(root, set(dirs) & set(owned.get("dirs", [])) | {ENGINE_DIR.as_posix(), ARCHIVE.as_posix(), ARCHIVE.parent.as_posix()})
    for line in report:
        print(line)
    kept = sum(1 for line in report if line.startswith("kept"))
    print(f"uninstalled CGSpec; kept {kept} changed files")
    return EXIT_OK


def remove_empty_dirs(root: Path, rels: set[str]) -> None:
    for rel in sorted(rels, key=lambda d: d.count("/"), reverse=True):
        path = root / rel
        if not path.is_dir() or path.is_symlink():
            continue
        keep = path / ".gitkeep"
        if keep.is_file() and [p.name for p in path.iterdir()] == [".gitkeep"]:
            keep.unlink()
        try:
            path.rmdir()
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="install.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "upgrade", "verify", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        if name == "upgrade":
            p.add_argument("--force", action="store_true", help="allow a downgrade")
            from _lib.upgrade_v1 import add_cli_args
            add_cli_args(p, PLUGIN_SOURCE)
        if name == "install":
            p.add_argument("--profile", required=True,
                           choices=["website", "web-workspace", "mobile", "cli", "headless", "small"])
            p.add_argument("--topology", required=True,
                           choices=["single", "monorepo", "large-monorepo", "polyrepo-governance", "polyrepo-member"])
            p.add_argument("--tools", default="git", help="comma-separated: claude,antigravity,codex,opencode,cursor,git,ci")
            p.add_argument("--repo-prefix", default="")
            p.add_argument("--plugin-source", default=PLUGIN_SOURCE, help="git URL CI downloads releases from")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"install": cmd_install, "upgrade": cmd_upgrade, "verify": cmd_verify, "uninstall": cmd_uninstall}
    try:
        root = Path(args.root).resolve()
        if not root.is_dir():
            raise CgspecError(f"not a directory: {root}")
        return handlers[args.command](root, args)
    except CgspecError as exc:
        print(f"install: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
