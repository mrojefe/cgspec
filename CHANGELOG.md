# CGSpec Methodology Changelog

All notable changes, architectural invariants, and skill evolutions to `cgspec` are documented in this file.

---

## [Unreleased]

### Added
- Declare-once agent-side capabilities (#53): an optional `docs/_cgspec/capabilities.json` registry (new `capabilities` schema) lists browser/account access, model delegation, human-contact channels and other tools the agent may use, each with an executable check. `cgspec_hook.py capability set|agree|check|show` drafts, approves (fresh message naming capabilities, or signed trailer) and probes it. Slice prerequisites can reference a capability with `capability: CAP-...` and `check: null`; `start ready` runs the registry check and hash-binds the registry, and a missing, unapproved, undeclared or failing capability is BLOCKED. Existing prerequisites are unchanged.
- Claude Code quota supervision (#91): an event-only status-line writer atomically records documented five-hour and seven-day provider readings only after the main-session request counter advances, preserves existing status-line output through chaining, and keeps the volatile snapshot out of Git. Session-start health warnings and non-blocking post-tool mode notices use monotonic five-minute reminders for unavailable/emergency states; Codex and Antigravity collection remains separate work.

## [v0.3.0] — 2026-10-29

### Added
- Named test runners bind each acceptance criterion to an approved command, environment, dependency set and structured result. Provisioning failures stay distinct from assertion failures, and mock substitution can be disabled with `CGSPEC_ALLOW_MOCKS=0`.
- Reproducible red-green proof executes tests at their recorded revisions in clean, provisioned worktrees, verifies semantic test identity and dependency closure, and rejects dirty or incomplete evidence.
- Safe test evolution records approved architectural supersession without rewriting frozen history; replacement tests must inventory old and new behavior and replay before historical proof can be skipped.
- Built-application parity checks compare frozen prototypes with production assets, journeys, screenshots and video at the exact candidate revision while keeping generated evidence outside the checkout.
- Per-slice Git execution plans bind approved bases, worktrees, file ownership, red/implementation/green ancestry, integration order and exact-candidate review across single- and multi-repository work.
- Merge-resilient proof seals map reviewed evidence through merge, squash, rebase and merge-queue outcomes, requiring signed revalidation when material content changes. Key isolation and protected-CI deployment remain live-environment responsibilities.
- Outcome-based closure (`cgspec_hook.py close check|checklist`): every active outcome needs current-revision passing evidence, real-environment proof where agreed, and an independent final review with no unresolved findings; stale evidence, mocks and self-review are rejected. Runs frozen for an agreed slice enforce this at `run complete`; historical runs are unaffected. Writes a per-outcome human verification checklist. Evidence schema gains review fields.
- Read-only adoption discovery (`cgspec_hook.py adopt discover`) builds an observed project map with observed/inferred/unknown/unreachable facts per repo, and `adopt ready` judges only a slice's required repos, reporting unreachable ones as BLOCKED with owner and timing instead of a silent PASS. New `adoption-map` schema.
- Slice success/readiness agreements now bind explicit outcomes and evaluation to fresh approval, executable probes, per-outcome coverage, and editor/staged/CI enforcement. T0 declarations require lightweight verification. `CGSPEC_ALLOW_MOCKS=0` disables mock substitution by default without changing real-proof requirements.
- Semantic Versioning release governance: a project declares its release policy in `project.json` (`release.enabled`, `pre_1_0`, `current_version`; `enabled: false` opts out), each included slice records its compatibility impact in a `Release:` field, and `release check --target VERSION` rejects an undersized target while naming the slice that caused the required bump. Backward-compatible deprecations count as at least a minor bump.
- The canonical method specification now has the version-independent name `docs/METHOD.md`; repository entry points distinguish current package code, consumer documentation, historical records and untracked benchmark sources.

## [v0.2.3] — 2026-10-24

### Added
- Passing evidence records now state the claim and data source. Commit and doctor checks reject fixture-backed or unknown-source records that claim real-backend integration. This checks the consistency of the record; it does not authenticate an agent's assertion that a backend was used.

### Fixed
- Local browser checks now prefer the checked project's Playwright package, while trusted CI continues to load its external pinned copy. Baseline approval refuses symlinked storage or scratch artifacts and malformed cached screenshots before replacing them with a fresh capture.

### Maintained
- CI still reports `--no-rerun` as incomplete verification, including when no new failing-test record appears in the range.

## [v0.1.5] — 2026-10-17
### Fixed
- **Law 9 Single-Supervisor Delegation Invariant:** Permanently eliminated debater subagents appearing in the user's main chat window. In Antigravity, any subagent spawned directly by the Main Agent sends its turn completion message back to the primary chat. The Main Agent is now **strictly forbidden from putting debater subagents in its `Subagents` array**. The Main Agent spawns **EXACTLY ONE subagent: `Council Supervisor & Inspector`**. The Supervisor executes the 3 orthogonal lenses (`debater-1`, `debater-2`, `debater-3`) directly in the disk workspace and streams only 3-line status updates to the Main Agent.
- **Zero Decorative Emoji Policy:** Codified a strict rule in `AGENTS.md` and skills forbidding AI agents from decorating chat text, headers, and bullet points with emojis (`🏛️`, `📌`, `⚖️`, `👥`, `📍`, `✅`). Enforces calm, clean, senior engineering typography.

### Added
- **Law 10 Phased Design Locking Funnel:** Banned jumping directly from a user prompt to final interactive code. Mandated a 4-stage locked design funnel (Stage 1: OOUX & Spatial Zoning $\to$ Stage 2: Tokens & Aesthetic Direction $\to$ Stage 3: 7-State Component Matrix $\to$ Stage 4: Versioned Prototype Baseline), conducting one-question-at-a-time interviews.
- **Four Breakthrough Taste Archetypes:** Codified in `skills/cgspec-ux/references/anti-slop-codex.md` (Traycer Luxury, Langfuse Calibrated Blueprint, UX Pilot Architectural Atelier, Cloudflare Daylight Infrastructure).

---

## [v0.1.4] — 2026-10-16
### Fixed
- **Council Subagent Isolation (Silent Debater Protocol):** Eliminated chat pollution where Debaters 1, 2, and 3 sent direct completion messages to the Main Agent. Debaters now write exclusively to disk (`docs/.working/council/COUNCIL-NNN-<slug>/debater-k-round-0k.md`) and remain completely silent in chat.
- **Council High-Information Subject Briefing:** Fixed omission where Main Agent launched council with an empty subagent ID table. Mandated that the Main Agent must state the **Locked Question**, the **Underlying Tension & Trade-offs**, and the **Target Invariant** in chat before spawning.
- **Law 7 Atomic ADR Gate:** Added an unskippable gate preventing AI agents from taking "memlog-only" shortcuts. If an architectural or security decision is made, the `docs/adr/ADR-NNN-<slug>.md` file must be written and committed in the exact same turn before advancing to the next stage.

---

## [v0.1.3] — 2026-10-16
### Added
- **Anti-Slop UI & UX Codex (`skills/cgspec-ux/references/anti-slop-codex.md`):** Formulated strict 10 Negative Constraints (banning purple gradients, unstyled Inter/Arial, 3-column circle-icon boxes, centered fluffy cards, bubbly radii) and 8 Positive Craft Standards (1px hairlines, tonal surface elevation, monospaced metadata, `<kbd>` shortcuts, 7-state UI lifecycle).
- **Universal Anti-Slop Master Prompt:** Portable prompt block compatible with Claude, Cursor, v0.dev, ChatGPT, and Antigravity.
- **Prototype Staging Lab Arena (`docs/.working/prototypes/staging/`):** Interactive browser testbed with responsive viewport toggles (1440px / 1024px / 375px), live 10-point Anti-Slop Scorecard, and side-by-side comparison.

---

## [v0.1.2] — 2026-10-16
### Added
- **Law 9: Real 4-Subagent Council Architecture:** Mandated that the Main Agent stays with the human and spawns 4 background subagents (1 dedicated neutral Supervisor/Host + 3 Debaters). Prohibited single-agent simulation.
- **Prototype Versioning Invariant:** Mandated immutable milestone baselines (`prototype-v0.1.html`), ADR-specific exploratory mockups (`prototype-ADR-NNN.html`), and `prototype.html` as the canonical pointer.

---

## [v0.1.1] — 2026-10-16
### Added
- **Law 7: Architectural Choices Produce ADRs (`docs/adr/`):** Every cross-cutting choice must create an ADR registered in `docs/00-INDEX.md`. Pre-flight ADR check mandated.
- **Law 8: Deterministic Sprouting with Numeric Preservation:** Sprouting from single files to directories only occurs when `wc -l > 500` or in `v0.2+` multi-squad, retaining two-digit numeric prefixes (`01-PRD/`, `02-NOUNS-AND-FLOWS/`).

---

## [v0.1.0] — 2026-10-16
### Added
- Initial release of CGSpec methodology: 5-spine architecture (`01-PRD` .. `05-EXPERIENCE`), `docs/.memlog.md` with CLI tool, and skill suite (`cgspec-init`, `cgspec-forge`, `cgspec-council`, `cgspec-slice`, `cgspec-pivot`, `cgspec-ux`).
