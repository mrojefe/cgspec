"""Templates and text snippets for tool adapters (workflow, selector script, instructions, cursor rule)."""

from __future__ import annotations

WORKFLOW = """name: cgspec-governance
on:
  push:
  pull_request:
permissions:
  contents: read
  issues: read
jobs:
  governance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          fetch-depth: 0
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: "3.12"
      - name: Pick the base and a trusted engine (never the engine inside this commit)
        env:
          EVENT_BASE: ${{{{ github.event.pull_request.base.sha || github.event.before }}}}
          EVENT_NAME: ${{{{ github.event_name }}}}
          EVENT_REF: ${{{{ github.ref }}}}
          DEFAULT_BRANCH: ${{{{ github.event.repository.default_branch }}}}
        run: |
          set -eu
          # Run the selection script as it was BEFORE this change, so a change cannot rewrite its own judge.
          B="$EVENT_BASE"
          if [ -n "$B" ] && [ "$B" != "$(git rev-parse HEAD)" ] &&
             git cat-file -e "$B:.github/cgspec-select.sh" 2>/dev/null; then
            TRUSTED="$B"
            git show "$B:.github/cgspec-select.sh" > "$RUNNER_TEMP/cgspec-select.sh"
          else
            # A default-branch push with no usable before SHA has no older trust root.
            if [ "$EVENT_NAME" != "push" ] || [ -z "$DEFAULT_BRANCH" ] ||
               [ "$EVENT_REF" = "refs/heads/$DEFAULT_BRANCH" ]; then
              echo "CGSpec: no trusted selector before this change; refusing this push" >&2
              exit 1
            fi
            # Do not read FETCH_HEAD after a failed fetch: it may name a stale object.
            if ! git fetch -q origin "refs/heads/$DEFAULT_BRANCH"; then
              echo "CGSpec: cannot fetch the default branch selector" >&2
              exit 1
            fi
            TRUSTED="$(git rev-parse 'FETCH_HEAD^{{commit}}')"
            if [ "$TRUSTED" = "$(git rev-parse HEAD)" ] ||
               ! git cat-file -e "$TRUSTED:.github/cgspec-select.sh" 2>/dev/null; then
              echo "CGSpec: default branch has no prior trusted selector" >&2
              exit 1
            fi
            git show "$TRUSTED:.github/cgspec-select.sh" > "$RUNNER_TEMP/cgspec-select.sh"
          fi
          CGSPEC_TRUST_BASE="$TRUSTED" sh "$RUNNER_TEMP/cgspec-select.sh"
      - name: CGSpec governance (cannot be skipped)
        id: proof
        env:
          CGSPEC_PROOF_DIR: ${{{{ runner.temp }}}}/cgspec-proof
          GH_TOKEN: ${{{{ github.token }}}}
        run: python3 "$CGSPEC_RELEASE/engine/cgspec_hook.py" ci --root "$GITHUB_WORKSPACE" --base "$CGSPEC_BASE" --release-dir "$CGSPEC_RELEASE" --tool git
      - name: Retain structured proof on success or failure
        if: ${{{{ always() && steps.proof.outcome != 'skipped' }}}}
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02  # v4.6.2
        with:
          name: cgspec-ci-proof
          # Only engine-written red/green files; the proof directory is hidden from the
          # candidate's own processes so a test cannot write a forged result beside them.
          path: |
            ${{{{ runner.temp }}}}/cgspec-proof/*-red.json
            ${{{{ runner.temp }}}}/cgspec-proof/*-green.json
          if-no-files-found: ignore
          retention-days: 14
      # CGSPEC_VISUAL_JOB
"""

SELECT = """#!/bin/sh
# Installed by CGSpec. Chooses the range base and downloads the plugin release used to judge it.
# Version = the newer of trusted selector commit and HEAD; source = that trusted commit.
set -eu
TRUST_BASE="${CGSPEC_TRUST_BASE:-${EVENT_BASE:-}}"
if [ -z "$TRUST_BASE" ] ||
   ! git cat-file -e "$TRUST_BASE:.github/cgspec-select.sh" 2>/dev/null ||
   ! git cat-file -e "$TRUST_BASE:docs/_cgspec/project.json" 2>/dev/null; then
  echo "CGSpec: no trusted selector and plugin source before this change" >&2
  exit 1
fi
BASE="${EVENT_BASE:-}"
if [ -z "$BASE" ] || ! git cat-file -e "$BASE:docs/_cgspec/project.json" 2>/dev/null; then
  BASE="$(git merge-base HEAD "$TRUST_BASE" 2>/dev/null || true)"
fi
if [ -z "$BASE" ] || [ "$BASE" = "$(git rev-parse HEAD)" ]; then
  echo "CGSpec: cannot establish a trusted range base before HEAD" >&2
  exit 1
fi
read_at() { git show "$1:$2" 2>/dev/null || true; }
field() { python3 -c "import json,sys; d=sys.stdin.read(); print(json.loads(d).get('$1','') if d.strip() else '')"; }
HEAD_VERSION="$(read_at HEAD docs/_cgspec/01-engine/VERSION | tr -d '[:space:]')"
TRUST_VERSION="$(read_at "$TRUST_BASE" docs/_cgspec/01-engine/VERSION | tr -d '[:space:]')"
SOURCE="$(read_at "$TRUST_BASE" docs/_cgspec/project.json | field plugin_source)"
if [ -z "$TRUST_VERSION" ] || [ -z "$SOURCE" ]; then
  echo "CGSpec: trusted selector lacks a version or plugin source" >&2
  exit 1
fi
VERSION="$(printf '%s\\n%s\\n' "$TRUST_VERSION" "$HEAD_VERSION" | grep . | sort -t. -k1,1n -k2,2n -k3,3n | tail -1)"
RELEASE="${RUNNER_TEMP:-/tmp}/cgspec-release"
rm -rf "$RELEASE"
git clone -q --depth 1 --branch "v$VERSION" "$SOURCE" "$RELEASE"
echo "CGSPEC_BASE=$BASE" >> "$GITHUB_ENV"
echo "CGSPEC_RELEASE=$RELEASE" >> "$GITHUB_ENV"
echo "CGSpec: base ${BASE:-none}, release v$VERSION from $SOURCE"
"""

GITHUB_STEPS = """To make the CGSpec check impossible to bypass, do this once on GitHub (spec 8, trust root):
  1. Settings > Branches (or Rules): protect your main branch, require the 'governance' status check.
  2. Require your review for changes to .github/, docs/_cgspec/ and docs/slices/
     (CODEOWNERS with your account, plus 'Require review from Code Owners').
  3. Do not give the AI agent your GitHub admin credentials.
  4. For squash/rebase proof, configure a protected CI SSH signing identity in project.json
     proof_signers and run `cgspec_hook.py proof seal|map` only in a trusted job after candidate
     processes have ended. Keep the private key outside the checkout and never expose it to tests.
  Optional: sign approvals with a hardware key that needs your touch and list it in
  project.json approver_keys."""

CURSOR_RULE = """---
description: CGSpec project rules
alwaysApply: true
---
This project uses CGSpec. Read docs/_cgspec/01-engine/CONTRACT.md before acting. Cursor has no
hooks, so the git hooks and CI enforce the rules; run doctor.py before ending a task.
"""
