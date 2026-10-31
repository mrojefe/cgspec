# CGSpec
Created: unknown · Last edited: 2026-10-31T20:15:00Z

[![Version](https://img.shields.io/badge/version-0.3.4-2563eb?style=flat-square)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.10%2B-059669?style=flat-square)](engine/)
[![Hosts](https://img.shields.io/badge/hosts-Claude_Code_%7C_Codex_%7C_Antigravity-7c3aed?style=flat-square)](plugin.json)
[![Enforcement](https://img.shields.io/badge/enforcement-Deterministic_Hooks_%26_CI-d97706?style=flat-square)](docs/METHOD.md#8-enforcement-engine)

**Deterministic Spec-Driven Software Engineering for AI Agents.**

When building software with AI coding agents, projects frequently degrade as they grow: agents forget earlier architectural choices, silently alter scope, claim tests passed without running them, or ship user interfaces that drift from the approved design.

**CGSpec solves this through a strict division of labor:**
1. **You speak in ordinary language:** You set goals, constraints, and taste, and approve consequential choices. You never need to memorize the method.
2. **The agent carries the method:** It maps what exists, brings sourced evidence and design references, maintains living architecture records, slices the work, writes failing tests before code, and records gaps honestly.
3. **Deterministic scripts enforce the rules:** Every rule that a machine can verify is enforced by local Git/tool hooks and CI (`engine/`) — never left to an AI's goodwill. Human judgment stays with you.

> [!IMPORTANT]
> The canonical [Method Specification](docs/METHOD.md) defines the full contract, repository layout, 6-layer UI/UX design path, review rules, adoption flow, and limits. [Implementation Status](docs/STATUS.md) separates implemented engine checks from outcomes that still require live proof.

---

## How CGSpec Works (The 5 Pillars)

```mermaid
flowchart TB
    subgraph HUMAN["1. YOU (Human Authority — Plain Language)"]
        H1["Set Goals, Constraints & Taste"]
        H2["Approve Consequential Choices & Final Outcomes"]
    end

    subgraph AGENT["2. AI AGENT (Carries the CGSpec Method)"]
        P1["Pillar 1: Idea & Sourced Research"]
        P2["Pillar 2: Shared Vision & 6-Layer UI/UX Design"]
        P3["Pillar 3: Slice Execution (Test-First Red -> Green)"]
        P4["Pillar 4: Scales from 1 Page to Polyrepo"]
        P1 --> P2 --> P3 --> P4
    end

    subgraph ENGINE["3. DETERMINISTIC ENGINE (Pillar 0 — Machine Enforcement)"]
        G1["Pre-Tool & Commit Gates<br/>(cgspec_hook.py)"]
        G2["Red-Green Ancestry & Proof Seals<br/>(verify.py / parity.py)"]
        G3["Health, Drift & Quota Guards<br/>(doctor.py / quota.py)"]
    end

    H1 ==>|"Ordinary words"| P1
    P2 ==>|"Visual prototypes & trade-offs"| H2
    H2 ==>|"Signed approval record"| P3
    P1 -.->|"Checked by"| ENGINE
    P2 -.->|"Checked by"| ENGINE
    P3 ==>|"Gated at every commit & push"| ENGINE
    ENGINE ==>|"Verified evidence checklist"| H2

    classDef humanBox fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef agentBox fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef engineBox fill:#d1fae5,stroke:#059669,stroke-width:2px,color:#064e3b
    class H1,H2 humanBox
    class P1,P2,P3,P4 agentBox
    class G1,G2,G3 engineBox
```

| Pillar | What It Guarantees |
| --- | --- |
| **0 — Deterministic Feedback** | The agent turns checkable rules into scripts under `docs/_cgspec/04-checks/` and runs them on every change. AI judgment is used only for what no script can measure. |
| **1 — Idea & Research** | Identifies goal type first (`money`, `learning`, `personal`, `client`). A commercial goal requires a sourced market research record before design begins. |
| **2 — Shared Vision & Design** | The agent leads with up to 6 real design references, helps articulate taste, and walks a fixed 6-layer UI path from user tasks to motion before coding. |
| **3 — Build (Test-First)** | Requires a failing test commit (`RED`) before implementation (`GREEN`), small single-purpose files, and zero duplicated logic (DRY). |
| **4 — Any Size** | Scales uniformly from a single-page site to a multi-service polyrepo while keeping each repository independent. |

---

## Why CGSpec? (Ungoverned AI vs. CGSpec Governance)

| Dimension | Ungoverned AI Coding | With CGSpec |
| --- | --- | --- |
| **Project Memory** | Lost across chat sessions; repeated prompting | Versioned in **5 Living Spines**, **ADRs**, and a structured state ledger |
| **Test Discipline** | Tests written after code, mocked away, or skipped | Enforced **Red -> Implementation -> Green** Git commit ancestry |
| **UI / UX Quality** | Generic layouts and silent visual regressions | Approved HTML prototypes + **Playwright visual parity** (`desktop`/`mobile`, `light`/`dark`) |
| **Code Review** | Same agent grades its own homework | **Independent cross-model review** + outcome-based human closure checklist |
| **Safety & Scope** | Accidental `git reset --hard`, leaked secrets, out-of-scope edits | **Pre-tool & pre-commit gates** block destructive Git, secrets, emojis, and unapproved scope |

---

## Repository Architecture: The 5 Spines & Engine State

When CGSpec is installed into a project, it structures architectural knowledge and machine verification into three clear zones:

```mermaid
flowchart LR
    subgraph SPINES["The 5 Living Spines (docs/spines/)"]
        S1["01-INTENT<br/>Why & Who"]
        S2["02-OOUX<br/>Domain Objects & Nouns"]
        S3["03-EXPERIENCE<br/>Flows & 6 UI Layers"]
        S4["04-ARCHITECTURE<br/>System, Data & Security"]
        S5["05-DELIVERY<br/>Environments & CI"]
        S1 --> S2 --> S3 --> S4 --> S5
    end

    subgraph RECORDS["Bounded Decisions & Work"]
        ADR["docs/adrs/<br/>Architectural Decisions"]
        PROTO["docs/prototypes/<br/>Visual Authority"]
        SLICE["docs/slices/<br/>Bounded Feature Slices"]
    end

    subgraph STATE["Engine & Cryptographic State (docs/_cgspec/)"]
        ENG["01-engine/<br/>Vendored Python Verifiers"]
        CHK["04-checks/<br/>Project Custom Rules"]
        ST["07-state/<br/>Approvals, Runs & Proof Seals"]
    end

    SPINES ==> RECORDS
    RECORDS ==> STATE

    classDef spineNode fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#2e1065
    classDef recNode fill:#e0f2fe,stroke:#0284c7,stroke-width:2px,color:#0c4a6e
    classDef stateNode fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    class S1,S2,S3,S4,S5 spineNode
    class ADR,PROTO,SLICE recNode
    class ENG,CHK,ST stateNode
```

---

## Deterministic Slice Lifecycle (`cgspec-slice`)

Every feature or fix is executed as a bounded **Slice** (`SLICE-XXX`) with explicit outcomes (`OUT-1`, `OUT-2`, ...). No slice can be closed on a passing unit test alone:

```mermaid
flowchart LR
    A["1. Slice Freeze &<br/>Capability Preflight"] --> B["2. RED Commit<br/>Failing Test Proven"]
    B --> C["3. GREEN Commit<br/>Bounded Implementation"]
    C --> D["4. Runtime & Visual Parity<br/>Playwright + Registered Checks"]
    D --> E["5. Independent Review<br/>Cross-Model Verification"]
    E --> F["6. Outcome Closure<br/>SSH-Signed Proof Seal"]

    classDef stepPrep fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef stepRed fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#7f1d1d
    classDef stepGreen fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef stepVerify fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef stepSeal fill:#f3e8ff,stroke:#9333ea,stroke-width:2px,color:#3b0764

    class A stepPrep
    class B stepRed
    class C stepGreen
    class D,E stepVerify
    class F stepSeal
```

---

## Collaboration Contract & Packaged Workflows

### Who Does What

| Area | You Decide | The Agent Does Alone | The Agent Must Ask You |
| --- | --- | --- | --- |
| **Product** | Goals, audience, scope | Turns plain words into intent, actors, nouns, flows; surfaces gaps | Conflicting goals or scope growth |
| **Design** | Taste, priorities, chosen direction | Finds up to 6 real references, builds HTML prototypes, checks visual parity | Choice between real design alternatives |
| **Architecture** | Constraints (cost, hosting, privacy, licenses) | Researches, runs spikes, drafts ADRs and spines | Consequential or hard-to-reverse trade-offs |
| **Delivery** | What ships first | Slices, failing tests, code, runtime proof, review packets | Irreversible actions (data deletion, production deploy, publishing) |
| **Method** | Nothing | Follows CGSpec, repairs drift, updates records, respects quota budget | Never burdens you with method plumbing |

### The 9 Workflows (`skills/`)

Skills under `skills/` load on demand for the task at hand (`cgspec-pivot` is included as a compatibility alias), supported by eight bounded team role definitions under `agents/`:

| Skill | When to Use It | What It Produces |
| --- | --- | --- |
| `cgspec-init` | Starting a brand-new project from an idea | Initializes `docs/spines/`, `docs/_cgspec/01-engine/`, hooks, and initial intent |
| `cgspec-adopt` | Bringing an existing codebase under CGSpec governance | Read-only discovery, legacy baseline snapshot (`SLICE-000`), and staged adoption |
| `cgspec-forge` | Shaping product intent, domain nouns (OOUX), and system architecture | Updates Spines `01` to `05` and records architectural decisions (`ADR-XXX`) |
| `cgspec-ui-ux` | Designing or refining user interfaces before implementation | Sourced visual references, interactive HTML prototypes, and Playwright journeys |
| `cgspec-council` | Resolving high-stakes architectural or product trade-offs | Structured multi-perspective analysis with explicit recommendation |
| `cgspec-slice` | Building a bounded feature or fix end-to-end | Frozen slice spec, Red-Green commits, runtime/visual proof, and outcome closure |
| `cgspec-migrate` | Schema, data, or cross-service migrations | Rollback-safe migration plan, verification scripts, and supersession records |
| `cgspec-operate` | Day-2 operations, health diagnostics, quota, and recovery | Runs `doctor.py`, `quota.py`, checkpoint recovery, and incident verification |
| `cgspec-team-review` | Auditing a slice or release candidate before closure | Independent reviewer verdict bound to the exact 40-hex Git revision |

---

## Navigation Map

| If you want to... | Read |
| --- | --- |
| Understand the complete method and its rules | [Current Method Specification](docs/METHOD.md) |
| Check what is implemented vs. what still needs live rehearsal | [Implementation Status](docs/STATUS.md) |
| Read the short operating contract installed into projects | [Short Agent Contract](engine/CONTRACT.md) |
| Inspect engine CLI commands, state files, and JSON schemas | [Engine Formats & Commands](engine/FORMATS.md) |
| Contribute to CGSpec itself | [Contributor Entry](AGENTS.md) |
| Browse historical v0.1 records, ADRs, and research benchmarks | [Documentation Map](docs/00-INDEX.md) |

---

## Quickstart & Installation

### 1. Try CGSpec in a Disposable Sandbox

```bash
mkdir -p /tmp/cgspec-example
python3 engine/install.py install --root /tmp/cgspec-example --profile web-workspace --topology single --tools git
python3 engine/doctor.py --root /tmp/cgspec-example
python3 engine/install.py verify --root /tmp/cgspec-example
```

### 2. Install or Upgrade the Host Plugin

Keep the host plugin and the project's vendored engine (`docs/_cgspec/01-engine/`) in step: update the plugin directory first, then run the project `upgrade` command below.

- **Claude Code:** `claude plugin update cgspec@<marketplace>`
- **Antigravity:** `agy plugin install <plugin-directory>` (re-import/reinstall when updating the directory)
- **Codex:** `codex plugin marketplace add <plugin-directory>` and install `cgspec@cgspec-local` (or `codex plugin marketplace upgrade <marketplace>` for a Git marketplace)

The root `plugin.json` and `.agents/plugins/marketplace.json` package the same ten skills for Claude Code, Codex, and Antigravity.

### 3. Upgrade an Installed Project

For an existing v0.2/v0.3 project, refresh its vendored engine, Claude/Antigravity hooks, and workspace skills:

```bash
python3 <current-plugin>/engine/install.py upgrade --root <project>
python3 <current-plugin>/engine/install.py verify --root <project>
python3 <current-plugin>/engine/doctor.py --root <project>
```

Replaced skill copies are archived automatically under `docs/_cgspec/_archive/agent-skills/`.

> [!NOTE]
> For legacy v0.1 layouts, `install.py upgrade --from-v1 --root <project>` produces a staged migration report before publication and stops where legacy links or decisions need human review. Do not pass `--apply` on a live project until the report, rollback, and ownership are settled.

### 4. Validate This Repository

```bash
python3 -m unittest discover -s tests -t .
claude plugin validate . --strict
```

First installation of governance on a GitHub repository requires an owner-reviewed baseline before the CGSpec check can gate protected branches. The exact trust sequence and the limits of local hooks are detailed in [Section 8 of the Method Specification](docs/METHOD.md#8-enforcement-engine): no local script alone proves a message came from the human when the agent controls the same shell.

CortexGate is optional and independent of CGSpec; using this method does not require that service.
