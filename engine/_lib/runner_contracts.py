"""Approved, stable runner definitions shared by commit and CI proof gates."""

from __future__ import annotations

import hashlib
import json

from .common import CgspecError
from .globs import match_any

_LOCKFILE_SUFFIXES = ("-lock.json", "lock.json", ".lock", "yarn.lock", "pnpm-lock.yaml",
                      "poetry.lock", "Pipfile.lock", "Cargo.lock", "Gemfile.lock", "go.sum")


def _is_lockfile(path: str) -> bool:
    return any(path.endswith(suffix) for suffix in _LOCKFILE_SUFFIXES)


# Resolving or lock-mutating verbs per package manager. Locked installs
# (npm ci, yarn/pnpm/poetry/bundle install, uv sync, pipenv sync) are allowed.
_UNLOCKED_ACTIONS = {
    "npm": ("install", "i", "add", "update", "up", "uninstall", "remove"),
    "yarn": ("add", "up", "upgrade", "update", "remove"),
    "pnpm": ("add", "update", "up", "remove"),
    "pip": ("install",), "pip3": ("install",),
    "uv": ("add", "remove"),
    "pipenv": ("install", "update"),
    "poetry": ("add", "remove", "update"),
    "bundle": ("update",),
    "cargo": ("add", "update"),
    "go": ("get",),
}


def _unlocked_install(environment: dict) -> str | None:
    """Reject dependency resolution or lock mutation that can differ between revisions.

    Scans every configured command phase (setup, build, readiness, services, cleanup) so a
    resolver cannot be moved out of setup to evade the lock, and normalizes common aliases
    (`npm i`/`npm add`, `yarn upgrade`, `uv add`, `pipenv install`) to the same verbs.
    """
    phases = [environment.get("setup", [])] + list(environment.get("build", []))
    phases += [environment.get("readiness", [])] + list(environment.get("services", []))
    if environment.get("cleanup"):
        phases.append(environment["cleanup"])
    for argv in phases:
        if not isinstance(argv, list) or not argv:
            continue
        command = argv[0].split("/")[-1]
        sub = argv[1] if len(argv) > 1 else ""
        if command in ("python3", "python", "python2", "python2.7") and len(argv) > 2 and argv[1] == "-m":
            command, sub = argv[2].split("/")[-1], (argv[3] if len(argv) > 3 else "")
        if sub not in _UNLOCKED_ACTIONS.get(command, ()):
            continue
        if command in ("pip", "pip3") and ("-r" in argv or "--no-deps" in argv):
            continue
        return f"{command} {sub} resolves or mutates dependencies without a pinned install"
    return None



def _semantic_errors(runner: dict, name: str) -> list[str]:
    """Validate a runner's optional semantic adapter."""
    semantics = runner.get("semantics")
    if semantics is None:
        return []
    if not isinstance(semantics, dict) or semantics.get("adapter") != "command-json-v1":
        return [f"runner {name}: unknown semantic test adapter"]
    reasons = []
    command = semantics.get("command", [])
    if (not isinstance(command, list) or
            sum(arg.count("{test_file}") for arg in command if isinstance(arg, str)) != 1 or
            sum(arg.count("{test_id}") for arg in command if isinstance(arg, str)) != 1):
        reasons.append(f"runner {name}: semantic command must select exactly one {{test_file}} and {{test_id}}")
    inputs = semantics.get("inputs", [])
    if (not isinstance(inputs, list) or not inputs or
            any(not isinstance(path, str) or any(char in path for char in "*?[") for path in inputs)):
        reasons.append(f"runner {name}: semantic adapter inputs must be non-empty literal file paths")
    return reasons

def errors(project: dict) -> list[str]:
    if not isinstance(project, dict):
        return ["project configuration must be an object"]
    runners = project.get("test_runners", [])
    environments = project.get("test_environments", [])
    if not isinstance(runners, list) or not isinstance(environments, list):
        return ["test_runners and test_environments must be arrays"]
    if not runners:
        return ["test_runners needs at least one runner"] if "test_runners" in project else []
    reasons = []
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in runners + environments):
        return ["test_runners and test_environments need string IDs and object entries"]
    runner_ids = [item.get("id") for item in runners if isinstance(item, dict)]
    env_ids = [item.get("id") for item in environments if isinstance(item, dict)]
    if len(set(runner_ids)) != len(runner_ids):
        reasons.append("test_runners IDs must be unique")
    if len(set(env_ids)) != len(env_ids):
        reasons.append("test_environments IDs must be unique")
    if len(runners) > 1 and any(not isinstance(runner.get("test_globs"), list) or
                                not runner.get("test_globs") for runner in runners):
        reasons.append("multiple test_runners must each declare non-empty test_globs")
    for runner in runners:
        if not isinstance(runner, dict):
            reasons.append("test_runners entries must be objects")
            continue
        name = runner.get("id", "unnamed")
        if runner.get("environment") not in env_ids:
            reasons.append(f"runner {name} references an undefined environment")
        command = runner.get("command", [])
        if not isinstance(command, list) or sum(arg.count("{test_file}") for arg in command if isinstance(arg, str)) != 1:
            reasons.append(f"runner {name} command must select exactly one {{test_file}}")
        if runner.get("report") == "json" and not any("{report}" in arg for arg in command if isinstance(arg, str)):
            reasons.append(f"runner {name} needs a {{report}} output path")
        if runner.get("report") == "stdout-json" and any("{report}" in arg for arg in command if isinstance(arg, str)):
            reasons.append(f"runner {name}: stdout-json cannot use a {{report}} file path")
        inputs = next((env.get("inputs", []) for env in environments if env.get("id") == runner.get("environment")), [])
        if not isinstance(inputs, list) or any(not isinstance(path, str) or any(char in path for char in "*?[")
                                              for path in inputs):
            reasons.append(f"runner {name}: environment inputs must be literal file paths")
        if runner.get("adapter") == "legacy-exit":
            reasons.append(f"runner {name}: exit-code-only proof remains legacy; use an assertion-aware adapter")

        reasons.extend(_semantic_errors(runner, name))
    for environment in environments:
        network = environment.get("network", "online")
        if network not in ("online", "offline-cache"):
            reasons.append(f"environment {environment['id']}: network must be online or offline-cache")
        if network == "offline-cache" and not any(_is_lockfile(path) for path in environment.get("inputs", [])):
            reasons.append(f"environment {environment['id']}: offline-cache needs a pinned lockfile input")
        unlocked = _unlocked_install(environment)
        if unlocked:
            reasons.append(f"environment {environment['id']}: {unlocked}")
    return reasons


def resolve(project: dict, runner_id: str, reader=None) -> tuple[dict, dict, str]:
    problems = errors(project)
    if problems:
        raise CgspecError("; ".join(problems))
    runner = next((r for r in project.get("test_runners", []) if r["id"] == runner_id), None)
    if runner is None:
        raise CgspecError(f"unknown test runner {runner_id!r}")
    environment = next(e for e in project["test_environments"] if e["id"] == runner["environment"])
    input_hashes = {}
    semantic_inputs = runner.get("semantics", {}).get("inputs", []) if reader else []
    for rel in dict.fromkeys(environment["inputs"] + semantic_inputs):
        raw_input = reader(rel) if reader else None
        if raw_input is None:
            raise CgspecError(f"runner {runner_id}: approved runner input {rel} is missing from the candidate")
        input_hashes[rel] = hashlib.sha256(raw_input).hexdigest()
    raw = json.dumps({"runner": runner, "environment": environment, "inputs": input_hashes}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return runner, environment, hashlib.sha256(raw).hexdigest()


def fingerprints(runner: dict, environment: dict, reader) -> dict:
    """Hash revision-specific runner, environment and semantic-adapter inputs."""
    inputs = {rel: hashlib.sha256(reader(rel)).hexdigest() for rel in dict.fromkeys(environment["inputs"] + runner.get("semantics", {}).get("inputs", []))}
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {"runner": hashlib.sha256(canonical(runner)).hexdigest(),
            "environment": hashlib.sha256(canonical(environment)).hexdigest(), "inputs": inputs}


def selected_command(runner: dict, test_file: str) -> list[str]:
    return [part.replace("{test_file}", test_file) for part in runner["command"]]


def pair_errors(project: dict, record: dict, reader=None) -> list[str]:
    """A new proof selects exactly the approved command and versioned runner."""
    runner_id = record.get("runner_id")
    if not runner_id or not record.get("runner_hash") or not record.get("selector"):
        return ["named proof requires runner_id, runner_hash and selector"]
    try:
        runner, _, digest = resolve(project, runner_id, reader)
    except CgspecError as exc:
        return [str(exc)]
    problems = []
    if record["runner_hash"] != digest:
        problems.append(f"runner {runner_id}: configuration hash differs from approved runner/environment")
    if record["selector"] != record.get("test_file"):
        problems.append(f"runner {runner_id}: selector must equal the recorded test_file")
    if record.get("command") != selected_command(runner, record.get("test_file", "")):
        problems.append(f"runner {runner_id}: command or selection differs from approved configuration")
    test_file = record.get("test_file", "")
    if runner.get("test_globs") and not match_any(test_file, runner["test_globs"]):
        problems.append(f"runner {runner_id}: test_file is outside its approved test_globs")
    project_test_globs = project.get("path_classes", {}).get("tests", [])
    if project_test_globs and not match_any(test_file, project_test_globs):
        problems.append(f"runner {runner_id}: test_file is outside path_classes.tests")
    semantics, recorded = runner.get("semantics"), record.get("test_semantics")
    if not semantics:
        problems.append(f"runner {runner_id}: proof needs an approved semantic adapter; dependency impact is unknown")
    elif not recorded:
        problems.append(f"runner {runner_id}: proof needs a semantic fingerprint and complete dependency closure")
    elif recorded and recorded.get("adapter") != semantics.get("adapter"):
        problems.append(f"runner {runner_id}: proof semantic adapter differs from approved configuration")
    for path in record.get("covers", []):
        if not match_any(path, runner.get("applies_to", [])):
            problems.append(f"runner {runner_id}: {path} is outside its approved scope")
    return problems
