"""Adapters: Antigravity payloads, and install wiring for Claude Code, Antigravity, git, CI and Cursor."""

from __future__ import annotations

import json
import os
import subprocess
import re
import unittest

from tests.helpers import TempDir, git, hook, install, run_script


def ag_payload(root, name, **args):
    return {"conversationId": "c1", "workspacePaths": [str(root)], "toolCall": {"name": name, "args": args}, "stepIdx": 3}


class AntigravityPayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        install(self.root)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def run_hook(self, payload, event="pre-tool"):
        return hook(self.root, event, payload, "--tool", "antigravity")

    def test_denied_write_answers_with_json_decision(self):
        result = self.run_hook(ag_payload(self.root, "write_to_file",
                                          TargetFile=str(self.root / "docs/_cgspec/01-engine/doctor.py"), CodeContent="x"))
        self.assertEqual(result.returncode, 0)
        answer = json.loads(result.stdout)
        self.assertEqual(answer["decision"], "deny")
        self.assertIn("protected", answer["reason"])

    def test_allowed_write_prints_nothing_blocking(self):
        result = self.run_hook(ag_payload(self.root, "replace_file_content",
                                          TargetFile=str(self.root / "docs/spines/01-INTENT.md"), ReplacementContent="text"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"decision": "allow"})

    def test_session_start_injects_pack_as_json(self):
        result = self.run_hook({"conversationId": "c1", "workspacePaths": [str(self.root)]}, "session-start")
        self.assertEqual(result.returncode, 0)
        steps = json.loads(result.stdout)["injectSteps"]
        self.assertIn("CGSpec session pack", steps[0]["systemMessage"]["systemMessage"])

    def test_post_tool_returns_empty_json_object(self):
        result = self.run_hook(ag_payload(self.root, "replace_file_content",
                                          TargetFile=str(self.root / "docs/spines/01-INTENT.md")), "post-tool")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {})

    def test_stop_allows_json_when_checkpoint_is_current(self):
        result = self.run_hook({"conversationId": "c1", "workspacePaths": [str(self.root)], "fullyIdle": True}, "stop")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["decision"], "allow")

    def test_run_command_uses_cwd(self):
        snap = self.root / "docs/prototypes/_snapshots"
        payload = ag_payload(self.root, "run_command", CommandLine="sed -i s/a/b/ ../../_memlog.md", Cwd=str(snap))
        self.assertEqual(json.loads(self.run_hook(payload).stdout)["decision"], "deny")

    def test_emoji_found_in_any_content_field(self):
        payload = ag_payload(self.root, "multi_replace_file_content", TargetFile=str(self.root / "docs/spines/01-INTENT.md"),
                             ReplacementChunks=[{"TargetContent": "a", "ReplacementContent": "b \U0001F680"}])
        self.assertEqual(json.loads(self.run_hook(payload).stdout)["decision"], "deny")

    def test_stop_keeps_agent_running_with_continue(self):
        (self.root / "docs/random.md").write_text("x\n")
        result = self.run_hook({"conversationId": "c1", "workspacePaths": [str(self.root)], "fullyIdle": True}, "stop")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["decision"], "continue")


def step_script(workflow: str, name: str) -> str:
    """The `run: |` body of the workflow step called `name`, dedented, as the runner would execute it."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line[10:])
    return "\n".join(body) + "\n"


class WiringTests(unittest.TestCase):
    TOOLS = "claude,antigravity,codex,opencode,git,ci,cursor"

    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        git(self.root, "init", "-q", "-b", "main")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def do_install(self):
        return run_script("install.py", "install", "--root", str(self.root), "--profile", "web-workspace",
                          "--topology", "single", "--tools", self.TOOLS)

    def test_all_adapters_are_wired(self):
        result = self.do_install()
        self.assertEqual(result.returncode, 0)
        self.assertIn("require the 'governance' status check", result.stdout)
        claude = json.loads((self.root / ".claude/settings.json").read_text())
        self.assertIn("PreToolUse", claude["hooks"])
        self.assertIn("cgspec_hook.py pre-tool --tool claude", json.dumps(claude))
        ag = json.loads((self.root / ".agents/hooks.json").read_text())
        self.assertIn("cgspec", ag)
        self.assertIn("PreToolUse", ag["cgspec"])
        codex = json.loads((self.root / ".codex/hooks.json").read_text())
        self.assertIn("PreToolUse", codex)
        self.assertIn("cgspec_hook.py pre-tool --tool codex", json.dumps(codex))
        opencode = json.loads((self.root / ".opencode/hooks.json").read_text())
        self.assertIn("PreToolUse", opencode)
        self.assertIn("cgspec_hook.py pre-tool --tool opencode", json.dumps(opencode))
        self.assertTrue(os.access(self.root / ".git/hooks/pre-commit", os.X_OK))
        self.assertTrue((self.root / ".git/hooks/commit-msg").exists())
        workflow = (self.root / ".github/workflows/cgspec.yml").read_text()
        self.assertIn("engine/cgspec_hook.py\" ci", workflow)
        self.assertIn("  issues: read\n", workflow)
        self.assertIn("          GH_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("CI visual proof (PR head)", workflow)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in
                            re.findall(r"uses: [^@\s]+@([0-9a-f]+)", workflow)))
        self.assertIn('visual_summary.py" --revision "$PR_HEAD"', workflow)
        frozen = json.loads((self.root / "docs/_cgspec/07-state/frozen.json").read_text())
        self.assertIn(".github/workflows/cgspec.yml", [e["path"] for e in frozen["entries"]])
        self.assertIn("CONTRACT.md", (self.root / ".cursor/rules/cgspec.mdc").read_text())
        self.assertEqual(run_script("doctor.py", "--root", str(self.root)).returncode, 0)

    def test_visual_job_points_the_engine_at_a_pinned_playwright_outside_the_checkout(self):
        from _lib.ci_visual import PLAYWRIGHT_VERSION
        self.assertEqual(self.do_install().returncode, 0)
        workflow = (self.root / ".github/workflows/cgspec.yml").read_text()
        self.assertRegex(PLAYWRIGHT_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertIn(f'"playwright@{PLAYWRIGHT_VERSION}"', workflow)
        script = step_script(workflow, "Install pinned Playwright outside the checkout")
        with TempDir() as tmp:
            # npm is broken on this runner: the engine must still be told to use only the trusted directory.
            (tmp / "bin").mkdir()
            (tmp / "bin/npm").write_text("#!/bin/sh\nexit 1\n")
            os.chmod(tmp / "bin/npm", 0o755)
            env = dict(os.environ, RUNNER_TEMP=str(tmp), GITHUB_ENV=str(tmp / "env"),
                       PATH=f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}")
            result = subprocess.run(["bash", "-c", script], cwd=self.root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            env_lines = (tmp / "env").read_text().strip().splitlines()
            self.assertIn(f"CGSPEC_PLAYWRIGHT_DIR={tmp / 'cgspec-playwright'}", env_lines)
            self.assertIn(f"PLAYWRIGHT_BROWSERS_PATH={tmp / 'cgspec-playwright-browsers'}", env_lines)
            self.assertFalse((self.root / "package.json").exists())

    def test_playwright_browsers_path_is_exported_in_the_proof_step_too(self):
        # docs/STATUS.md says Chromium installs under $RUNNER_TEMP: both the install step (checked above,
        # via GITHUB_ENV) and the proof step (which actually runs Chromium) must set it, not just one.
        self.assertEqual(self.do_install().returncode, 0)
        workflow = (self.root / ".github/workflows/cgspec.yml").read_text()
        script = step_script(workflow, "CI visual proof (PR head)")
        self.assertRegex(script, r'export PLAYWRIGHT_BROWSERS_PATH="\$RUNNER_TEMP/cgspec-playwright-browsers"')

    def test_upload_step_is_gated_on_the_proof_step_outcome_and_targets_runner_temp(self):
        from _lib.ci_visual import PROOF_STEP_ID
        self.assertEqual(self.do_install().returncode, 0)
        workflow = (self.root / ".github/workflows/cgspec.yml").read_text()
        proof_start = workflow.index('- name: CI visual proof (PR head)')
        self.assertIn(f"id: {PROOF_STEP_ID}", workflow[proof_start:])
        upload_block = workflow[workflow.rindex("uses: actions/upload-artifact@"):]
        self.assertIn(f"steps.{PROOF_STEP_ID}.outcome == 'success'", upload_block)
        self.assertIn(f"steps.{PROOF_STEP_ID}.outcome == 'failure'", upload_block)
        condition = next(line for line in upload_block.splitlines() if line.strip().startswith("if:"))
        self.assertNotIn("skipped", condition, "skipped proof steps must not upload candidate artifacts")
        path_line = next(line for line in upload_block.splitlines() if line.strip().startswith("path:"))
        self.assertRegex(path_line, r"path:\s*\$\{\{\s*runner\.temp\s*\}\}/cgspec-visual-proof-")
        self.assertNotIn("artifacts/cgspec-visual", path_line)

    def test_upload_artifact_is_pinned_to_a_supported_major_by_full_sha(self):
        from _lib.ci_visual import UPLOAD_ARTIFACT_SHA, UPLOAD_ARTIFACT_VERSION
        self.assertRegex(UPLOAD_ARTIFACT_SHA, r"^[0-9a-f]{40}$")
        self.assertGreaterEqual(int(UPLOAD_ARTIFACT_VERSION.split(".")[0]), 4)
        self.assertEqual(self.do_install().returncode, 0)
        workflow = (self.root / ".github/workflows/cgspec.yml").read_text()
        pins = re.findall(r"uses: actions/upload-artifact@(\S+)", workflow)
        self.assertEqual(pins, [UPLOAD_ARTIFACT_SHA, UPLOAD_ARTIFACT_SHA])
        self.assertIn(f"actions/upload-artifact@{UPLOAD_ARTIFACT_SHA}  # v{UPLOAD_ARTIFACT_VERSION}", workflow)

    def test_governance_upload_keeps_only_generated_proof_after_attempt(self):
        self.assertEqual(self.do_install().returncode, 0)
        workflow = (self.root / ".github/workflows/cgspec.yml").read_text()
        governance = workflow[workflow.index("- name: CGSpec governance (cannot be skipped)"):]
        upload = governance[:governance.index("- name: Install pinned Playwright")]
        self.assertIn("id: proof", upload)
        self.assertIn("CGSPEC_PROOF_DIR: ${{ runner.temp }}/cgspec-proof", upload)
        self.assertIn("steps.proof.outcome != 'skipped'", upload)
        self.assertIn("cgspec-proof/*-red.json", upload)
        self.assertIn("cgspec-proof/*-green.json", upload)
        self.assertNotIn("cgspec-proof/*.json", upload)

    def test_headless_profile_has_no_visual_job(self):
        root = self.root
        result = run_script("install.py", "install", "--root", str(root), "--profile", "headless",
                            "--topology", "single", "--tools", "ci")
        self.assertEqual(result.returncode, 0, result.stderr)
        workflow = (root / ".github/workflows/cgspec.yml").read_text()
        self.assertNotIn("CI visual proof", workflow)

    def test_antigravity_hook_resolves_engine_from_agents_working_directory(self):
        self.assertEqual(self.do_install().returncode, 0)
        data = json.loads((self.root / ".agents/hooks.json").read_text())
        command = data["cgspec"]["PreToolUse"][0]["hooks"][0]["command"]
        payload = ag_payload(self.root, "replace_file_content",
                             TargetFile=str(self.root / "docs/spines/01-INTENT.md"),
                             ReplacementContent="text")
        result = subprocess.run(command, shell=True, cwd=self.root / ".agents", input=json.dumps(payload),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("can't open file", result.stderr)

    def test_antigravity_hook_resolves_engine_before_git_init(self):
        with TempDir() as root:
            result = run_script("install.py", "install", "--root", str(root), "--profile", "web-workspace",
                                "--topology", "single", "--tools", "antigravity")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / ".agents/hooks.json").read_text())
            command = data["cgspec"]["PreToolUse"][0]["hooks"][0]["command"]
            payload = ag_payload(root, "replace_file_content", TargetFile=str(root / "docs/spines/01-INTENT.md"),
                                 ReplacementContent="text")
            called = subprocess.run(command, shell=True, cwd=root / ".agents", input=json.dumps(payload),
                                    capture_output=True, text=True)
            self.assertEqual(called.returncode, 0, called.stderr)
            self.assertEqual(json.loads(called.stdout)["decision"], "allow")
            from_root = subprocess.run(command, shell=True, cwd=root, input=json.dumps(payload),
                                       capture_output=True, text=True)
            self.assertEqual(from_root.returncode, 0, from_root.stderr)
            self.assertEqual(json.loads(from_root.stdout)["decision"], "allow")

    def test_upgrade_refreshes_claude_hook_without_removing_user_hook(self):
        self.assertEqual(self.do_install().returncode, 0)
        path = self.root / ".claude/settings.json"
        data = json.loads(path.read_text())
        data["hooks"]["PreToolUse"].append({"matcher": "Read", "hooks": [
            {"type": "command", "command": "echo my-hook"}]})
        data["hooks"]["Stop"][0]["hooks"][0]["command"] = "python3 stale/cgspec_hook.py stop --tool claude"
        path.write_text(json.dumps(data))
        result = run_script("install.py", "upgrade", "--root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = json.loads(path.read_text())
        self.assertIn("echo my-hook", json.dumps(after))
        self.assertIn('"${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"/docs/_cgspec/01-engine/cgspec_hook.py stop',
                      after["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertNotIn("stale/cgspec_hook.py", json.dumps(after))

    def test_existing_settings_are_merged_and_restored(self):
        (self.root / ".claude").mkdir()
        mine = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-check"}]}]}}
        (self.root / ".claude/settings.json").write_text(json.dumps(mine))
        self.do_install()
        merged = json.loads((self.root / ".claude/settings.json").read_text())
        self.assertEqual(merged["model"], "opus")
        self.assertIn("my-check", json.dumps(merged))
        self.assertIn("cgspec_hook.py", json.dumps(merged))
        run_script("install.py", "uninstall", "--root", str(self.root))
        after = json.loads((self.root / ".claude/settings.json").read_text())
        self.assertNotIn("cgspec_hook.py", json.dumps(after))
        self.assertIn("my-check", json.dumps(after))

    def test_foreign_git_hook_is_not_overwritten(self):
        hooks = self.root / ".git/hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\necho mine\n")
        result = self.do_install()
        self.assertEqual(result.returncode, 0)
        self.assertEqual((hooks / "pre-commit").read_text(), "#!/bin/sh\necho mine\n")
        self.assertIn("pre-commit", result.stdout + result.stderr)

    def test_real_git_hook_rejects_a_bad_commit(self):
        self.do_install()
        git(self.root, "add", "-A")
        env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e")
        first = subprocess.run(["git", "commit", "-q", "-m", "install cgspec"], cwd=self.root, capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (self.root / "src").mkdir()
        (self.root / "src/a.ts").write_text("export const a = 1;\n")
        git(self.root, "add", "-A")
        bad = subprocess.run(["git", "commit", "-q", "-m", "no tier"], cwd=self.root, capture_output=True, text=True, env=env)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("no change tier", bad.stdout + bad.stderr)

    def test_uninstall_unwires_everything_it_added(self):
        self.do_install()
        run_script("install.py", "uninstall", "--root", str(self.root))
        for rel in (".agents/hooks.json", ".codex/hooks.json", ".opencode/hooks.json",
                    ".git/hooks/pre-commit", ".github/workflows/cgspec.yml", ".cursor/rules/cgspec.mdc"):
            self.assertFalse((self.root / rel).exists(), rel)


class PortableRootResolutionTests(unittest.TestCase):
    def test_command_root_resolution(self):
        from _lib.adapters import _command
        claude_cmd = _command("pre-tool", "claude")
        self.assertIn("${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}", claude_cmd)
        codex_cmd = _command("pre-tool", "codex")
        self.assertIn("$(git rev-parse --show-toplevel 2>/dev/null || pwd)", codex_cmd)
        self.assertNotIn("CLAUDE_PROJECT_DIR", codex_cmd)
        opencode_cmd = _command("pre-tool", "opencode")
        self.assertIn("$(git rev-parse --show-toplevel 2>/dev/null || pwd)", opencode_cmd)
        generic_cmd = _command("pre-tool", "generic")
        self.assertIn("$(git rev-parse --show-toplevel 2>/dev/null || pwd)", generic_cmd)

    def test_portable_command_runs_from_subdirectory(self):
        with TempDir() as root:
            git(root, "init", "-q", "-b", "main")
            install(root)
            sub = root / "src/subdir"
            sub.mkdir(parents=True)
            from _lib.adapters import _command
            # Simulating Codex running from a subdirectory without CLAUDE_PROJECT_DIR set
            cmd = _command("tier show", "codex")
            env = dict(os.environ)
            env.pop("CLAUDE_PROJECT_DIR", None)
            res = subprocess.run(cmd, shell=True, cwd=sub, capture_output=True, text=True, env=env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("No such file or directory", res.stderr)
            # Claude command also resolves via git when CLAUDE_PROJECT_DIR is empty/unset
            claude_cmd = _command("tier show", "claude")
            res_claude = subprocess.run(claude_cmd, shell=True, cwd=sub, capture_output=True, text=True, env=env)
            self.assertEqual(res_claude.returncode, 0, res_claude.stderr)
            self.assertNotIn("No such file or directory", res_claude.stderr)


class CodexPayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        install(self.root)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def run_hook(self, payload, event="pre-tool", stdin=None):
        return hook(self.root, event, payload, "--tool", "codex")

    def test_denied_write_blocks_codex(self):
        payload = {
            "tool_name": "file_edit",
            "tool_input": {
                "file_path": str(self.root / "docs/_cgspec/01-engine/doctor.py"),
                "old_str": "import sys",
                "new_str": "import os",
            },
        }
        res = self.run_hook(payload)
        self.assertEqual(res.returncode, 2)
        self.assertIn("protected", res.stderr)

    def test_allowed_write_allows_codex(self):
        payload = {
            "tool_name": "file_edit",
            "tool_input": {
                "file_path": str(self.root / "docs/spines/01-INTENT.md"),
                "old_str": "a",
                "new_str": "b",
            },
        }
        res = self.run_hook(payload)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_apply_patch_denied_on_protected_file(self):
        patch = (
            "--- a/docs/_cgspec/01-engine/doctor.py\n"
            "+++ b/docs/_cgspec/01-engine/doctor.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-import sys\n"
            "+import os\n"
        )
        payload = {
            "tool_name": "apply_patch",
            "tool_input": {"patch": patch},
        }
        res = self.run_hook(payload)
        self.assertEqual(res.returncode, 2)
        self.assertIn("protected", res.stderr)

    def test_bash_dangerous_command_blocked_codex(self):
        payload = {
            "tool_name": "bash",
            "tool_input": {"command": "sed -i s/a/b/ docs/_memlog.md"},
        }
        res = self.run_hook(payload)
        self.assertEqual(res.returncode, 2)
        self.assertIn("protected", res.stderr)

    def test_codex_user_message_captures_approval(self):
        payload = {"prompt": "approve SLICE-001", "session_id": "codex-session-123"}
        res = self.run_hook(payload, event="user-message")
        self.assertEqual(res.returncode, 0)
        approvals = json.loads((self.root / "docs/_cgspec/07-state/approvals.json").read_text())["approvals"]
        self.assertEqual(len(approvals), 1)
        self.assertIn("approve SLICE-001", approvals[0]["excerpt"])
        inbox = (self.root / "docs/00-INBOX.md").read_text()
        self.assertIn("approve SLICE-001", inbox)
        self.assertIn("[sess sion-123]", inbox)

    def test_post_tool_codex_exits_zero(self):
        payload = {
            "tool_name": "file_edit",
            "tool_input": {"file_path": str(self.root / "docs/spines/01-INTENT.md")},
        }
        res = self.run_hook(payload, event="post-tool")
        self.assertEqual(res.returncode, 0)


class OpenCodePayloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        install(self.root)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def run_hook(self, payload, event="pre-tool"):
        return hook(self.root, event, payload, "--tool", "opencode")

    def test_denied_write_blocks_opencode(self):
        payload = {
            "tool": "edit",
            "args": {
                "filePath": str(self.root / "docs/_cgspec/01-engine/doctor.py"),
                "content": "new text",
            },
        }
        res = self.run_hook(payload)
        self.assertEqual(res.returncode, 2)
        self.assertIn("protected", res.stderr)

    def test_allowed_write_allows_opencode(self):
        payload = {
            "tool": "edit",
            "args": {
                "filePath": str(self.root / "docs/spines/01-INTENT.md"),
                "content": "new text",
            },
        }
        res = self.run_hook(payload)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_bash_dangerous_command_blocked_opencode(self):
        payload = {
            "tool": "bash",
            "args": {"command": "sed -i s/a/b/ docs/_memlog.md"},
        }
        res = self.run_hook(payload)
        self.assertEqual(res.returncode, 2)
        self.assertIn("protected", res.stderr)

    def test_post_tool_opencode_exits_zero(self):
        payload = {
            "tool": "edit",
            "args": {"filePath": str(self.root / "docs/spines/01-INTENT.md")},
        }
        res = self.run_hook(payload, event="post-tool")
        self.assertEqual(res.returncode, 0)


class ContentProjectionTests(unittest.TestCase):
    def test_project_content_codex_str_replace(self):
        from _lib.gates import project_content
        with TempDir() as root:
            target = root / "src/sample.txt"
            target.parent.mkdir(parents=True)
            target.write_text("Hello world\nGood morning\n")
            payload = {
                "tool_name": "file_edit",
                "tool_input": {
                    "file_path": "src/sample.txt",
                    "old_str": "world",
                    "new_str": "universe",
                },
            }
            projected = project_content(root, "src/sample.txt", payload, "codex", None)
            self.assertEqual(projected, "Hello universe\nGood morning\n")

    def test_project_content_codex_unified_diff(self):
        from _lib.gates import project_content
        with TempDir() as root:
            target = root / "src/sample.txt"
            target.parent.mkdir(parents=True)
            target.write_text("line1\nline2\nline3\n")
            patch = (
                "--- a/src/sample.txt\n"
                "+++ b/src/sample.txt\n"
                "@@ -1,3 +1,3 @@\n"
                " line1\n"
                "-line2\n"
                "+line2_updated\n"
                " line3\n"
            )
            payload = {
                "tool_name": "apply_patch",
                "tool_input": {"patch": patch},
            }
            projected = project_content(root, "src/sample.txt", payload, "codex", None)
            self.assertEqual(projected, "line1\nline2_updated\nline3\n")

    def test_project_content_opencode_edit(self):
        from _lib.gates import project_content
        with TempDir() as root:
            target = root / "src/sample.txt"
            target.parent.mkdir(parents=True)
            target.write_text("alpha beta gamma\n")
            payload = {
                "tool": "edit",
                "args": {
                    "filePath": "src/sample.txt",
                    "old_string": "beta",
                    "new_string": "delta",
                },
            }
            projected = project_content(root, "src/sample.txt", payload, "opencode", None)
            self.assertEqual(projected, "alpha delta gamma\n")


class ApprovalCaptureCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.root = self._tmp.__enter__()
        install(self.root)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_capture_approval_via_positional_arg(self):
        res = run_script("cgspec_hook.py", "capture-approval", "approve SLICE-004", "--root", str(self.root))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("captured approval A-", res.stdout)
        data = json.loads((self.root / "docs/_cgspec/07-state/approvals.json").read_text())
        self.assertEqual(len(data["approvals"]), 1)
        self.assertEqual(data["approvals"][0]["excerpt"], "approve SLICE-004")
        self.assertIn("approve SLICE-004", (self.root / "docs/00-INBOX.md").read_text())

    def test_capture_approval_via_message_flag(self):
        res = run_script("cgspec_hook.py", "capture-approval", "--message", "lgtm", "--root", str(self.root))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("captured approval A-", res.stdout)
        data = json.loads((self.root / "docs/_cgspec/07-state/approvals.json").read_text())
        self.assertEqual(len(data["approvals"]), 1)
        self.assertEqual(data["approvals"][0]["excerpt"], "lgtm")

    def test_capture_approval_via_stdin(self):
        res = run_script("cgspec_hook.py", "capture-approval", "--root", str(self.root), stdin="ship it\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("captured approval A-", res.stdout)
        data = json.loads((self.root / "docs/_cgspec/07-state/approvals.json").read_text())
        self.assertEqual(len(data["approvals"]), 1)
        self.assertEqual(data["approvals"][0]["excerpt"], "ship it")

    def test_capture_non_approval_records_inbox_and_fails(self):
        res = run_script("cgspec_hook.py", "capture-approval", "hello world, can you check this?", "--root", str(self.root))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("did not match an approval pattern", res.stderr)
        self.assertFalse((self.root / "docs/_cgspec/07-state/approvals.json").exists())
        self.assertIn("hello world", (self.root / "docs/00-INBOX.md").read_text())


if __name__ == "__main__":
    unittest.main()
