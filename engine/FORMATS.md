# CGSpec engine: file formats and hook contract
Created: unknown · Last edited: 2026-10-29T22:56:36Z

This file is the contract between the engine scripts, the hooks and the agent. The JSON
Schemas in `schemas/` are the machine-checked source; this page explains them. Spec section
numbers refer to `docs/METHOD.md`.

All state files carry `"schema": 1`. A future breaking change bumps it and ships a migration
in `install.py upgrade`.

### Hook activity and missing-hook detection

The six lifecycle callbacks (`session-start`, `pre-tool`, `post-tool`, `user-message`,
`stop`, `pre-compact`) record a machine-local receipt before their ordinary work.
`07-state/hook-heartbeats/<host-identity>.json` carries `schema: 1`, `event`, epoch-second
`at`, `session_id`, host `pid`, `host`, `sequence`, `previous` MAC and `mac`. A local
ignore file keeps these ephemeral receipts out of Git. Each host has its own file,
so concurrent sessions cannot cover for one another. Linux kernel ancestry, boot ID
and process start ticks determine host identity; caller-supplied session IDs and
timestamps cannot select it. Recording requires a matching host/event payload and
a direct callback process, optionally through one shell. All callbacks retain stdin.

Each receipt is HMAC-SHA256 authenticated and chained to the previous receipt. A
private, locked witness under the OS temporary directory, outside the checkout,
holds the random key and latest authenticated head for each host/project. Checking
requires both that witness and an identical authenticated checkout receipt. Editing
a timestamp, recreating an unkeyed chain, copying another process's receipt or
replaying an older signed receipt fails. Ordinary pre-tool edits and shell commands
that name receipt/witness paths are refused. Neither `session-check`, doctor nor
the Git commit event creates or refreshes hook receipts.

`cgspec_hook.py session-check --root <project>` automatically finds a supported live
Claude/Antigravity ancestor even when invoked from a tool shell in a session whose
settings never loaded. Missing activity fails immediately with an explicit
`HOOK COVERAGE FAILURE` diagnostic, process age and relaunch instructions. A receipt
older than 120 seconds also fails if Git reports edits/deletions after it; an idle
session alone does not fail. The installed Git pre-commit event independently runs
the same check before checking the staged snapshot. This is a local check, not a CI
requirement. An unavailable Git status fails visibly rather than asserting coverage;
a receipt writer failure blocks the next pre-tool action, while user messages and
finished tools remain non-blocking. Use `--tool claude` or `--tool antigravity` to
require verification even when host discovery is unavailable; a generic check outside a supported host prints
`UNVERIFIED` and continues its structural checks.

Limits: Linux `/proc` host discovery is currently required. No watchdog can warn if
neither an independent check nor the Git gate runs. A chain and a key accessible to
the same OS account are not an isolation boundary: unrestricted same-account shell
access can steal the key, alter the external witness or imitate callback ancestry.
The signal rejects hand-written heartbeat files; it does not authenticate human
approvals or certify every historical edit. These checks need an installed consumer
upgrade; a synthetic-host subprocess test is not a live Claude/Antigravity rehearsal.

### Git execution plans

`docs/slices/SLICE-NNN-*.git-plan.json` is the machine-checked execution record for a T1+
slice. `repositories` declares unique repository IDs, full approved base SHAs, branches,
optional worktree paths, outcome IDs and explicit file ownership. Parallel ownership also
declares one unique `worker_worktrees` path per owner; the editor gate requires the matching
`CGSPEC_WORKER_ID`. `branch_policy` has one default branch and optional stacked branches,
each with a dependency and reason. `commits` accepts only real commits in current history and
enforces red-test, implementation and green-test order. `review` binds passing checks to the
latest recorded candidate HEAD. The sidecar-only commit that stores this binding is a
traceability envelope around its reviewed parent; any later non-plan candidate change is stale.

For a multi-repository outcome, `multi_repo.repos` identifies exactly the repositories above
and carries each branch, candidate commit, pull-request reference and status. The same object
declares the shared outcome, contract version, integration order and cross-repository check.
The doctor validates these sidecars separately from slice Markdown.

### Named test runners

Existing projects can keep `test_command` and unchanged legacy test-pairs; installing named
runners does not rewrite either. To upgrade, add `test_environments`, `test_runners`, and a
fresh `test_runners_approval` naming `test_runners`, then record new proof with `runner_id`,
`selector`, and `runner_hash`. New unbound legacy proof is rejected after this migration.
Removing named runners is not a silent downgrade.

Each runner declares its ID, adapter, argument vector, working directory, test selection,
`applies_to` source globs, environment reference, timeout, isolation, report, and artifacts.
Optional `test_globs` select the test files for that runner. A single runner without them uses
`path_classes.tests`; multiple runners must each declare them so local pre-push execution cannot
send every test file to every framework. The selected files must still belong to
`path_classes.tests`.
An environment declares setup/readiness argument vectors, working directory, timeout,
literal input files and `network` (`online` by default, or `offline-cache`). Optional `build`
and `services` are arrays of argument vectors; `cleanup` is one argument vector.
Optional `variables` hold non-secret values, and `secrets` list required environment variable
names, never their values. Offline-cache requires a lockfile in `inputs`, and the operator
must provide its dependency cache and use a locked installer. A setup or build step that
resolves dependencies with an unlocked command (`npm install`, `pip install` without `-r`,
`yarn add`, `poetry add`, and similar) is rejected for every network mode. The engine sets
npm and pip offline flags but does not sandbox arbitrary network clients. The hash covers
runner and environment definitions plus the bytes of those inputs at the checked revision.
Missing inputs and changed definitions or inputs block proof; changing configuration requires
renewed approval.

Commands run directly as argument vectors, never through a shell, in a separate disposable
Git worktree and report directory for each run. `{test_file}` occurs exactly once; `{report}`
is supplied only for file reports. Structured `cgspec-json`, `vitest-json`, and
`playwright-json` reports distinguish assertions from setup, runtime, and malformed-report
failures; a concurrent runtime or unhandled error is never a valid red assertion. New
`legacy-exit` definitions are rejected. Every run checks out the full pinned commit,
installs/builds in that worktree, and verifies that setup did not add, modify, or delete any
file under the runner's `applies_to` scope or the selected test. Commands run in their own
process group, killed with descendants on timeout. Setup, build, readiness, test, service and
cleanup logs and outcomes survive worktree removal; cleanup failure overrides either test
verdict. The result records full revision, runner/environment/input fingerprints, test and
report hashes, commands, logs, and hashes of declared artifacts. Local proof JSON lives under
`.cgspec-scratch/ci-proof/`; the generated CI workflow instead writes to `$RUNNER_TEMP/cgspec-proof`
and uploads the engine-written red/green JSON after an attempted governance step, whether it
passes or fails. The proof directory is hidden from the candidate's own processes, and a
skipped step uploads nothing. Artifact patterns hash files but do not persist their bytes.

The real-tool fixture is `tests/fixtures/named-web/`; run its opt-in check with
`CGSPEC_RUN_WEB_INTEGRATION=1 python3 -m unittest tests.integration.test_named_web -v`.
It uses pinned Vitest/Playwright packages and installed Chrome when available. Ordinary
source tests skip this network/browser-dependent check.

### Merge-resilient proof seals

`docs/_cgspec/06-evidence/seals/<seal-id>.json` is a canonical signed envelope around one
test-pair and its trusted pre-integration execution. It binds outcome IDs, exact test-pair
bytes, reviewed base/head, assertion-failed and passing detail hashes, runner/environment/test
fingerprints, independent review, and relevant source, test, dependency, project and runner-input
hashes. `attestation` names a provider-independent identity, provider, protected workflow, run
and signing time. Its detached SSH signature uses namespace `cgspec-proof`. Verification resolves
that identity only from `proof_signers` at `reviewed_base`, never from candidate-added policy.
After bootstrap, changing `proof_signers` requires a commit signature from an already trusted
`approver_keys` key; the first signer policy requires the protected owner-reviewed baseline.

`docs/_cgspec/06-evidence/integrations/<map-id>.json` maps a seal digest to one integrated
revision, pull request and explicit strategy: `merge`, `squash`, `rebase` or `merge-queue`.
Every sealed path is hashed at the integrated revision. A material difference must name both
hashes and a second signed revalidation seal bound to that revision. Seal, revalidation and
mapping trust all remain pinned to the original protected `reviewed_base` policy; a candidate
cannot rotate authority during integration. Both record types are add-only; modification
or deletion fails the commit gate. Canonical repository records are durable; uploaded CI details
remain supplementary inputs.

Trusted CI creates records with `cgspec_hook.py proof seal` and `proof map`. The seal command
consumes engine-written red/green JSON plus a PASS review record bound to the green revision;
the map command computes differences itself and requires a path-to-revalidation JSON object for
any changed path. Both commands require a private SSH key path outside the checkout and verify
the generated signature against protected policy before writing output. Run signing in a separate
trusted job after candidate processes end; never make the key available to tests.

### Safe test evolution and architectural supersession

New named pairs also record `test_semantics`: `adapter: command-json-v1`, a 64-character
selected-test `fingerprint`, and `dependencies` entries with `path`, `kind` and core-computed
`sha256`. The approved runner declares `semantics.adapter`, an argument-vector `command`
with exactly one `{test_file}` and `{test_id}`, and nonempty literal `inputs`. Both runner
configuration hashes and persisted execution fingerprints include semantic-input bytes.
The adapter emits exactly `schema: 1`, `test_id`, `semantic_fingerprint`,
`closure_complete: true`, and `dependencies` with `path` and `kind` (`helper`, `fixture`,
`import`, `snapshot`, `config`). The core checks containment and hashes dependency bytes.
Language parsing is an adapter capability; the core contains no AST implementation.

CI compares the recorded identity at red and the pinned candidate. Unrelated additions
in the same file and adapter-established formatting changes may preserve identity; a
selected-test semantic change or changed dependency requires fresh proof. Whole-file
immutability applies only to legacy exit-code proof. Historical legacy and pre-semantic
pairs have unknown dependency closure: any later file change selects them for revalidation
and requires fresh semantic proof, or an approved replacement. Historical pair JSON is
immutable; do not rewrite its identity to fit changed code.

Freeze replacement runs with explicit `outcomes`, `proofs` paths, and `supersession` containing
`replaces`, `reason`, `approval_ref`, and `links`. Each link group (`agreements`, `outcomes`,
`tests`, `proofs`) is an array of `{old, new}` mappings covering the complete old inventory.
Omitted inventories remain unknown; explicit empty inventories cannot omit required slice
start outcomes. Slice-bound inventories must match their respective start record.
Approval must name `approve supersession R-YYYYMMDD-xxxx to R-YYYYMMDD-yyyy` (or use the
trusted commit-trailer approval path). Only a recorded active run may transition to
`superseded`; newly introduced terminal runs are rejected. Frozen fields and historical
proof hashes remain immutable after transition.

Replacement proof links must identify distinct schema-valid semantic test-pairs belonging
to the replacement run's tests and slice. Commit validation checks mappings and approval;
CI additionally calls `prove_failed()` on the replacement at the candidate before retiring
historical proof. Structurally approved retired pairs cannot cover later implementation.

## Files

Human-readable CGSpec Markdown documents (except the append-only inbox and ledger, engine files, and archived originals) show `Created: ... · Last edited: ...` immediately below the title. New template documents get both dates at install. The v0.1 upgrader uses `Created: unknown` when source creation cannot be proven. The commit gate rejects missing or unchanged edit dates on changed documents, future dates, new documents dated before install or marked unknown outside install/upgrade, date-only edits, invented creation dates on pre-existing documents, and changed creation across renames; doctor warns about untouched legacy documents missing the line. Date metadata does not count as substantive spine content.

| File (in a project) | Schema | Written by | Notes |
| --- | --- | --- | --- |
| `docs/_cgspec/project.json` | `project` | `install.py` | Version marker. `path_classes` globs drive T0 detection (spec 3). Optional `visual_drift_threshold` (default 0.5% of pixels) sets the threshold between W-DRIFT and E-DRIFT. Optional named `test_runners` and shared `test_environments` require targeted `test_runners_approval`. `proof_signers` lists protected CI SSH public keys with provider/workflow constraints. Optional `release` block declares the Semantic Versioning policy: `enabled` (true or false; false is an explicit opt-out), `pre_1_0` (`0.major.0`, `0.minor.0`, `0.patch.0`; default `0.minor.0`) and `current_version` (required while enabled). |
| `docs/_cgspec/07-state/tier.json` | `tier` | main agent via `cgspec_hook.py tier set` | Declared before editing. `files` and `verification` required for new T0 declarations; `slice` required for T1+. |
| `docs/_cgspec/capabilities.json` | `capabilities` | main agent via `cgspec_hook.py capability` | Optional. Declare-once agent-side capabilities (`CAP-...` IDs; `kind` `account_access`, `model_delegation`, `human_contact`, `tool_access`, `other`; `summary`, `usage`, `owner`, `assistance` and a required executable `check`). `approval` binds `registry_hash` (sha256 of the canonical `capabilities` list) to a fresh message naming capabilities or a signed trailer commit; editing the list makes the approval stale. Probe results are never stored here. |
| `docs/slices/SLICE-NNN-*.start.json` | `start` | main agent via `cgspec_hook.py start` | Versioned success/evaluation definition, fresh approval, executable readiness, active outcomes and mock-switch state. Definition edits invalidate readiness and require renewed approval. |
| `docs/_cgspec/05-runs/<run-id>/run.json` | `run` | inspector | `criteria`, `tests` and `proofs` hold sha256 values. Frozen inventories, outcomes and supersession mappings are immutable. New runs start active; retirement requires validated replacement links and approval. |
| `docs/_cgspec/07-state/frozen.json` | `frozen` | `bundle_prototype.py`, `cgspec_hook.py freeze`, `visual_audit.py --approve-baseline` | Append-only. CI rejects any change to an existing entry (spec 8, trust root). |
| `docs/_cgspec/04-checks/manifest.json` | `checks-manifest` | registration flow | Append-only for the same reason. A check runs as a gate only when listed here with a matching hash. Written only by `cgspec_hook.py check register` (script and optional Markdown guide must be regular files under `.cgspec-scratch/checks/`). Optional `guide` and `guide_sha256` pin the approved fix guidance beside the script; a missing or changed guide blocks execution. |
| `docs/_cgspec/07-state/checkpoint.json` | `checkpoint` | main agent via checkpoint command | `in_flight` is written before each batch and cleared after; clearing it is not project completion. Optional source observations and unresolved-stop diagnostics support recovery. |
| `docs/_cgspec/07-state/budget.json` | `budget` | hooks | Counts helpers, review rounds and research passes against limits. |
| `docs/_cgspec/07-state/quota.json` | `quota` | Host quota adapter; no collector/writer is included in this checkout | `quota.py --root <project> [--json]` and session packs validate freshness (five minutes), windows and thresholds. Missing or invalid readings give bounded advice; the command does not collect usage or enforce a task gate (spec 9). |
| `docs/_cgspec/06-evidence/tests/<test-id>.json` | `test-pair` | slice workflow | `failed_at` is the commit where the test failed. Unchanged legacy `test_command` proof replays exit codes and is labelled `legacy exit-code-only proof`; it does not meet the new strict assertion-aware contract. A new named proof additionally records `runner_id`, `runner_hash`, and `selector`; CI re-runs the approved runner at both revisions and checks structured assertion results. |
| `docs/_cgspec/06-evidence/seals/<seal-id>.json` | `proof-seal` | protected CI signing job | Immutable signed pre-integration proof. Trust is resolved at `reviewed_base`; candidate-added signers cannot authorize it. |
| `docs/_cgspec/06-evidence/integrations/<map-id>.json` | `proof-map` | protected CI signing job | Immutable signed envelope around the integrated parent. Changed sealed content requires an exact mapping and signed revalidation seal. |
| `docs/_cgspec/06-evidence/<scope>/<check>.json` | `evidence` | whoever ran the check | New PASS records name `claim` and `data_source`. A fixture or unknown source cannot support `claim: integration`; use `data_source: real_backend` only after exercising the real backend. When present, `runner_id`, `runner_hash`, `selector`, and `test_id` must all match a test-pair. `state` becomes `Stale` when a covered file changes. A label does not authenticate the runner or prove the backend was real. Review records use `claim: review` and add `reviewer`, `builder`, `reviewer_model`, `inspected` (diff/artifacts examined) and `findings` (`id`, `severity`, `status` open|resolved, `resolution`). `cgspec_hook.py close` reads these records. |
| `docs/prototypes/_baselines/<screen>-<viewport>.png` | baseline | `visual_audit.py --approve-baseline` | Approved visual baseline screenshot; read-only and recorded with sha256 in `frozen.json` (kind `baseline`). Protected against overwrite by write and commit gates. |
| `docs/prototypes/<screen>/journeys/<name>.json` | journey (checked by `journey.py`) | main agent at layer 4 | `name`, `url` (file next to the screen, or `http://` for the real frontend), optional `viewports` (`desktop`, `mobile`) and `covers`, and `steps`. Each step has one action on a `data-testid` (`click`, `fill`+`value`, `press`+`key`, `select`+`value`, `check`, `hover`, `goto`, `screenshot`) or `expect` with `visible`, `hidden`, `text`+`equals`/`contains`, `value`+`equals`, `count`+`equals`, `enabled`, `disabled`, `url`; a `state` label may go with any step. Evidence goes to `06-evidence/journeys/`, visual audits to `06-evidence/visual/`, registered checks to `06-evidence/checks/`. Journey names (and screen names) must be non-empty with no newlines or control characters. `visual_audit.py` compares current renders against approved baselines in `docs/prototypes/_baselines/` inside Chromium (reporting `W-DRIFT` or `E-DRIFT` when exceeding `visual_drift_threshold`, diff image in `.cgspec-scratch/visual/`), and renders in dark mode (`colorScheme: "dark"`, reporting info if identical to light or labeling dark contrast/focus failures `(dark)`). `verify.py` runs doctor, journeys and visual audit in one pass and prints a summary table. `journey.py` and `visual_audit.py` take `--json-out FILE` (`{"schema": 1, "tool", "records": [{"item", "verdict", "detail"}]}`); `visual_summary.py` turns those into the CI `evidence.json` and step summary. |
| `docs/_cgspec/07-state/legacy-baseline.json` | `legacy-baseline` | `cgspec-adopt` | Files listed here are exempt from size and test rules until their hash changes. |
| `docs/_cgspec/07-state/adoption-map.json` | `adoption-map` | `cgspec_hook.py adopt save`, or `adopt discover --json-out FILE` | Read-only discovery result mirroring `classify`: `generated_at`, `probe.git`, `repos` and `facts_kinds` counts. Each reachable repo records branch, full and short HEAD, dirty/untracked paths, worktrees, CGSpec marker, docs/tests/CI presence and a per-field fact kind (`observed`, `inferred` for present-but-unproven CI, `unknown`); a missing or non-git path is `{root, reachable: false, reason}`. Written only on request; `adopt discover` alone writes nothing. |
| `docs/_cgspec/07-state/approvals.json` | `approvals` | `user-message` hook only | One entry per user message that reads as an approval. A ledger entry with status Approved must cite one of these IDs (`--approval-ref A-...`). |
| `docs/_cgspec/07-state/stop-retries.json` | none | `stop` hook | Counts consecutive stop refusals; reset when the agent ends cleanly. |

## Git adapter ownership

`owned-files.json` is installer state, not user approval. Under `adapters`,
`git_hooks` stores only installed event names (`pre-commit`, `commit-msg`, `pre-push`).
Paths are discovered from the current Git checkout at operation time. Legacy
`git_abs` and `git` entries are read for recognized event names only, never as
filesystem targets; upgrade replaces them with `git_hooks`. Uninstall preserves
scripts that differ from the installed template. Linked worktrees use Git's
common directory and therefore share hooks; this is not per-worktree ownership.

## Approval provenance and local signatures

An inbox approval authorizes a change only when backed by verifiable hook
provenance. An approval entry in `docs/_cgspec/07-state/approvals.json` must carry
the host-supplied `session_id`, positive integer `captured_ns`, `excerpt`, and
`sha256` matching the message excerpt digest. The local commit gate verifies that
fresh approvals have valid session provenance, consistent capture timestamps, and
no replays of the same event across candidate or historical approvals. When
hook heartbeat records are present, the approval's session and capture time must
align with recorded, authenticated hook activity. Bare inbox lines or hand-edited
entries in `approvals.json` lack verifiable provenance and are rejected.

The agent contract strictly forbids invoking `cgspec_hook.py user-message`
directly from a shell, script, or wrapper. When hook capture is unavailable,
authorizations use the human signed-trailer path: the commit message carries
`Approved-by: Name <email>` and the commit is signed with a private key whose
public counterpart is listed in `project.json` `approver_keys`.

The signed-trailer signature check runs locally as well as in CI. Because
`commit-msg` runs before Git creates the signed commit object, the installed
`post-commit` hook immediately verifies the newly-created commit (`HEAD`)
against `approver_keys`, reporting any missing or invalid signatures.
The installed blocking `pre-push` hook verifies all outgoing commits against each
commit's parent policy, preventing any unsigned or unprovenanced changes from
being published. Upgrade installs these hooks while preserving custom hooks.

## IDs

| Kind | Form | Example |
| --- | --- | --- |
| Ledger entry | `M-YYYYMMDD-xxxx` (4 random hex) | `M-20261018-a3f9` |
| Tier declaration | `T-YYYYMMDD-xxxx` | `T-20261018-0c1d` |
| Run | `R-YYYYMMDD-xxxx` | `R-20261018-77be` |
| ADR / slice | `ADR-NNN-<slug>`, `SLICE-NNN-<slug>`; drafts `ADR-draft-<slug>` | `SLICE-004-login` |
| Registered check | `CHK-NNN` | `CHK-012` |

Polyrepo members prefix IDs with `repo_prefix:` (`api:ADR-004`). Legacy ledger IDs `M-001`
are still read so old projects can be upgraded.

## Ledger entry (`docs/_memlog.md`)

```
### [M-20261018-a3f9] 2026-10-18T21:40:00Z | tags: decision,design | owner: @ojefe | perms: public
- **Summary:** one line
- **Details:** optional
- **Status:** Delegated            (decisions only; spec 2 statuses)
- **Links:** ADR-004, SLICE-012    (optional)
```

Only `memlog.py` writes this file.

## Slice file fields read by the engine

```
# SLICE-004: Login form
- **Status:** Approved            (Draft, Approved, In progress, Done, Blocked)
- **Scope:** src/login/**, tests/login/**   (comma-separated globs; T1+ edits must match)
- **Release:** minor (public API) — new capability; no migration   (impact: none|patch|minor|major, then the affected public surface and rationale; read by `release check`)
```

Only slices in status Approved, In progress or Done are part of a release. Internal-only
migrations are not automatically major; an incompatible public data/export format always is.
A slice declaring `patch` while its rationale says the change deprecates a surface is treated
as at least `minor`.

## Outcome closure

`cgspec_hook.py close check|checklist --slice SLICE-NNN [--pr URL,...]` decides whether a slice's active outcomes
(from its #30 agreement) are demonstrated. Exit 2 lists every blocker. An outcome closes only with: a latest-per-check
`PASS` evidence record whose `covers` names it and whose `revision` is current (no change outside
`06-evidence/`, `07-state/` and `.cgspec-scratch/` since that revision, committed or not; `state: Stale` never counts);
no current `FAIL`, `BLOCKED` or `PARTIAL` record; and, when the agreement says `requires_real`, a `PASS` with
`claim: integration`, `data_source: real_backend` and `ran_by` other than the builder. Fixtures, mocks, static
evidence and self-declared labels cannot substitute. The slice also needs a current `claim: review` `PASS` record
covering every active outcome that names a `reviewer` different from the `builder`, a `reviewer_model`, a non-empty
`inspected` list, no `same_model_review`, and no finding that is open or resolved without a `resolution`. Model or
vendor names are never treated as proof of independence. `checklist` repeats the check and writes
`06-evidence/closure/SLICE-NNN.md`, organised by outcome with proof paths, the reviewed commit, PR links and the
accepted behaviours to confirm. Runs frozen for a slice that has an agreement carry `"closure": "required"` and
`run complete` enforces the same check; runs without the marker (historical records) complete as before.

## `cgspec_hook.py` command line

```
python3 docs/_cgspec/01-engine/cgspec_hook.py <event> [--tool TOOL] [--root PATH]
```

The tool's own hook payload (JSON) arrives on stdin; the adapter for each tool passes it through
and `--tool` tells the hook how to read it. Events:

| Event | When | Blocks? |
| --- | --- | --- |
| `session-start` | session start and after compaction | no; prints the session pack |
| `session-check` | inside `session-start`, also runnable alone | no; reports, exit 1 if broken |
| `user-message` | Claude's `UserPromptSubmit`, read directly from host-owned stdin with OS peer verification | never; scrubs secrets, saves to inbox, records approvals; refuses capture from shell pipes/redirects or an unverifiable host channel |
| `pre-tool` | before a write or shell tool | yes |
| `post-tool` | after a write, shell or helper-agent tool | no; marks evidence whose `covers` globs match a changed file `Stale`, counts helper agents in `budget.json` (exit 2 only to show a budget warning) |
| `pre-compact` | before compaction | no; captures bounded mechanical source context and prints the pack; does not invent semantic progress |
| `stop` | agent wants to end | yes, at most 2 times in a row (counter on disk) |
| `pre-commit` | git pre-commit | yes |
| `commit-msg` | git commit-msg | yes |
| `post-commit` | git post-commit; verifies finished approval commits immediately upon creation | no (reports local error) |
| `pre-push` | git pre-push; verifies finished approval commits on every outgoing ref update | yes |
| `ci` | CI governance stage | yes |
| `proof seal`, `proof map` | protected CI signing stages before and after integration | yes; refuse missing/untrusted inputs |

Registered checks: `pre-commit`, `ci`, `check run` and doctor verify every registered script and guide
against its manifest hash on every run, whether or not the check applies to the changed files; a missing
or changed one fails the gate and its check does not run. `ci` verifies this separately for every commit
in the range against that commit's own manifest and tree (through its `View`), not only the final
checked-out tree: a commit that tampers with an approved script or guide and a later commit in the same
range that restores the exact approved bytes still fails the range. Each check runs with two environment
variables set by the engine:

| Variable | `pre-commit` | `ci` | `check run` |
| --- | --- | --- | --- |
| `CGSPEC_BASE_REV` | `HEAD` | the trusted range base the workflow passes as `--base` (after the install-commit floor) | `HEAD` |
| `CGSPEC_REPO_ROOT` | the real repository | the checkout | the project |
| working directory (candidate content) | exported staged snapshot (no `.git`) | the checked-out HEAD | the working tree |

A check that compares against history reads the base with `git -C "$CGSPEC_REPO_ROOT" show
"$CGSPEC_BASE_REV:<path>"` and the candidate from its working directory. `templates/checks/no_new_suppressions.py`
does this and refuses to guess (exit 2) when the variables are missing.

Browser runs: `CGSPEC_BROWSER=none` forces BLOCKED; `CGSPEC_CHROMIUM` names a Chromium executable. When `CGSPEC_PLAYWRIGHT_DIR` is set (an absolute directory outside the project; the CI workflow sets it), Playwright is loaded only from `$CGSPEC_PLAYWRIGHT_DIR/node_modules`, never from the project's `node_modules` or `NODE_PATH`; otherwise the project's Playwright is used. A journey or audit passes only when the driver reported every expected viewport and these files exist with non-zero size and a PNG/WebM signature: for each viewport, `<label>-<viewport>.png` (final journey state, or the audited page), `<label>-<viewport>-dark.png` (audit dark render), `<label>-<viewport>-<name>.png` for each journey `screenshot` step, and `<label>-<viewport>.webm` with `--video`.

Document dates: `cgspec_hook.py doc sync` dates new documents, advances `Last edited` on edited ones, reverts date-only edits and labels edited legacy documents `Created: unknown`; the `stop` hook runs it at the end of each turn.

Tier declarations: `cgspec_hook.py tier set --tier T0 --reason "..." --files a,b --verify "..."` (T0) or
`--slice SLICE-NNN` (T1+); `tier show`; `tier clear`. CI: `cgspec_hook.py ci --base <sha>`.

Adoption: `cgspec_hook.py adopt discover [--repos a,b] [--json-out FILE]` prints the adoption map for the
root and each named repo (paths relative to `--root`); it runs only read-only git commands with
`--no-optional-locks` and writes nothing except the requested `--json-out` file. `adopt save` also writes
`07-state/adoption-map.json`. `adopt ready --slice SLICE-NNN [--repos a,b]` judges only the named repos
(default `.`): `OK` (git root with docs and tests observed), `PARTIAL` (reachable, some signal missing or
unknown; limited verification, never PASS), `BLOCKED` (missing or not a git root; owner `user`, timing
`before_start`) or `UNKNOWN` (reachability undeterminable). Exit 0 unless a named repo is BLOCKED or UNKNOWN,
then exit 2. Unnamed repos are never judged.

Success/readiness: `cgspec_hook.py start set --slice SLICE-NNN --file <draft.json>` writes a Draft sidecar;
`start agree --approval A-...` binds a fresh user approval (or `trailer` after the exact Draft is committed with a trusted signature in HEAD);
`start ready` executes approved probes; `start begin --outcome OUT-ID[,OUT-ID]` activates eligible outcomes; `start show`
prints the record, eligible outcomes and blockers. `CGSPEC_ALLOW_MOCKS=0` (default) blocks mock-based substitution;
`1` allows labelled development mocks; final evaluation remains separate and may still require real proof.
List local probe scripts/configuration in `check.inputs` (besides executable file arguments): readiness hashes them,
and staged/CI checks reject changed inputs. Inline/external commands still depend on the approved runner/environment;
trusted runner attestations and replay are later work. Probe stdout/stderr is not persisted. Message approvals must
follow the Draft; signed-trailer approval binds the exact signed Draft commit even across unrelated later commits.

Capabilities: `cgspec_hook.py capability set --file <draft.json>` writes the registry from a draft holding only
`{"capabilities": [...]}` and clears any approval; `capability agree --approval A-...` binds a fresh approval whose
message names capabilities (`approve capabilities`), or `trailer` after the exact draft is committed with a trusted
signature in HEAD; `capability check [--id CAP-a,CAP-b]` runs the checks now, prints per-capability `PASS`/`BLOCKED`
with the declared `usage`, and exits 2 when any is BLOCKED; it refuses an unapproved or invalid registry and persists
nothing. `capability show` prints the registry and whether it is approved. The registry is editor-protected, and
staged/CI checks validate it and its approval exactly like a slice agreement approval (single-use, fresh, targeted).
A start prerequisite may carry `capability: "CAP-..."`; it must then have `check: null`. `start ready` resolves the
capability from the approved registry and runs its check (cwd and inputs relative to the project root); the result's
`input_hashes` include `docs/_cgspec/capabilities.json` and the check's inputs, so registry edits after readiness
block `start begin` and staged implementation until readiness is rerun. A missing registry, invalid or unapproved
registry, or undeclared ID records a BLOCKED result naming the reason. Prerequisites without `capability` behave as
before.

Project.json may declare `implementation_capabilities: ["CAP-..."]`; these become synthetic
`REQ-PREFLIGHT-CAP-...` prerequisites covering every outcome. Their results also hash-bind project.json, and Ready or
In-progress records must cover the exact configured global set. `protected_branches` lists integration baselines that
may be approved bases but cannot be the default or a stacked implementation branch.

Release governance: `cgspec_hook.py release policy|plan|check --target VERSION [--slices SLICE-...]`.
`policy` prints the declared release block and its problems; `plan` prints the policy, every included
slice's classified impact and the maximum required bump with its causes; `check --target` rejects a
target below the computed requirement and names the slice that caused the bump. A project with no
`release` block is blocked (opt in or out explicitly — never guessed); `release.enabled: false` is an
explicit opt-out and passes with a note. `--slices` limits which slices count.

Per-tool payloads: `--tool claude` reads Claude Code hook JSON (`tool_name`, `tool_input`);
`--tool antigravity` reads `toolCall.name` / `toolCall.args` (`TargetFile`, `CommandLine`,
`Cwd`) and answers with `{"decision": "deny", "reason": ...}` on stdout (and `{"decision":
"continue"}` from `stop`) instead of exit 2; `--tool generic` reads
`{"tool": "write|edit|shell", "paths": [...], "content": ..., "command": ..., "cwd": ...}`.
Antigravity hooks receive no user message, so inbox and approval capture work only on Claude
Code; elsewhere approvals use the commit trailer path.

Exit codes (same for every engine script):

| Code | Meaning | Claude Code | git / CI |
| --- | --- | --- | --- |
| 0 | allow / pass | proceeds | passes |
| 1 | check failed (scripts) | shown, non-blocking | fails |
| 2 | blocked by a rule; reason on stderr | tool call refused, reason fed to the agent | fails |
| 3 | internal error in the engine | shown, non-blocking | fails |

`pre-tool` fails closed on internal errors only when the target path is protected (frozen,
engine, manifests, CI files); elsewhere it reports exit 3 so a bug in the engine cannot freeze
every session, and the commit hook and CI still catch the change.


## Checkpoint updates and recovery

`cgspec_hook.py checkpoint --next '...' --in-flight '...'` updates validated existing state. `--done`, `--blocked`, `--awaiting` and `--in-flight` are mutually exclusive. `--awaiting '<question>'` records `awaiting_user` (`at`, `question`, `used`, `count`) and keeps `in_flight`: the stop check accepts one allowed stop on an unused question, then marks it used and checks in-flight work normally again; a refused stop does not use it. A new `--awaiting` before any stop replaces the question. `--done`, `--blocked`, or a change of `in_flight` value (flag or patch) clears it; restating the same `in_flight` does not. Nothing is parked when it clears: hosts without a stop check never mark a question used, so `used` does not mean answered. `count` counts questions put to the user during the current in-flight batch and is lost when the batch ends; only hosts with a stop check increase it, because only they mark a question used. The session pack shows an unused question as asked, not as unanswered. The session pack shows the question, worded as still waiting or as last asked, so a resumed session sees what the user was asked. Damaged or missing state is preserved and requires explicit repair from known records; the command never replaces it with empty defaults.

`checkpoint --update-file <path>` (or `--update-file -` for stdin) accepts a JSON object containing semantic checkpoint fields. Use a unique file under `.cgspec-scratch/` or `/tmp`. Each named field replaces its entire value; omitted fields remain unchanged, arrays/maps replace rather than merge, and `null` is valid only for nullable schema fields. Derived `source`, `unresolved_stop`, `schema` and `updated` cannot be patched. Do not mix a patch key with a flag updating the same field. These commands assume one checkpoint writer; cross-process ownership remains pending.

`checkpoint --capture` saves a bounded source observation without advancing the semantic `updated` time. It records HEAD, branch, hashes of the installed project/engine manifests and eligible dirty worktree files. The pack compares it with current disk state without refreshing the saved baseline. This is recovery context, not evidence that tests ran or that staged bytes match tested bytes. Capture is not an atomic snapshot across concurrent edits.

Limits: Git commands have two-second timeouts and two-MiB output limits; capture hashes at most 200 dirty entries, two MiB per file and eight MiB total including identity files. Untracked directories are not enumerated. Missing Git, byte/count limits, unreadable/unstable files and omitted content produce explicit partial coverage. Sensitive-named paths (`.env*`, secret/credential names and private-key extensions) are omitted without names or hashes; this name heuristic does not discover all secrets. Symlinks and special files are not read. The saved exclusion list names skipped build/cache directories and transient hook files. Actual dependency lockfiles remain observed. An unchanged partial observation cannot establish an unchanged checkout.

The pre-compaction hook captures only mechanical context; semantic goals, pending work and evidence still require the agent's update before a long batch or context pressure. Unavailable hooks cannot guarantee a last-minute save. After two stop refusals, the bounded exit retains exact unresolved reasons and their own timestamp, preserving `in_flight` and semantic `updated`. If checkpoint persistence fails, diagnostics fall back to `stop-retries.json`; if disk writes also fail, exit remains bounded with an explicit SAVE FAILED warning. The next pack shows retained diagnostics. A clean stop clears only the derived stop record, without rewriting an already clean checkpoint.

These observations are not authenticated receipts or approvals. Direct checkpoint/stop-state tool edits are protected where the host's pre-tool hook detects them; shell parsing is partial. Run reservations, remote receipt reconciliation and cross-run writer exclusion are not implemented by this checkpoint repair. New optional fields remain readable by this engine, but older strict schema readers may reject them; release and installed-CI compatibility must be established before rollout.
