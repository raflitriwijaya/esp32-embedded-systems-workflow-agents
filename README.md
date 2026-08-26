# esp32-embedded-systems-workflow-agents

Claude Code agent framework for the **ESP32** platform, supporting the engineering workflow specified in `../workflow-iot/WORKFLOW_SECTION1.md` through `SECTION7.md`.

Sibling frameworks are expected for STM32 and Raspberry Pi. The stage model, gate checklists, and state schema they share are platform-independent — see "Multi-platform boundary" below.

**Separation of concerns.** `workflow-iot/` holds the *engineering specification* — what good practice looks like, independent of any tooling. This directory holds the *machinery* that makes an agent apply that specification: state schema, hooks, skills, subagents, and validators. The specification is readable by any engineer or auditor. This directory is only meaningful inside Claude Code.

---

## Layout

The directory structure mirrors the install target, so installation is a copy or symlink rather than a translation.

| Path | Installs to | Contents |
|---|---|---|
| `STAGE_STATE_SCHEMA.md` | — (reference only) | Specification of `stage-state.yaml` and `.stage-cache.json` |
| `MCP_SPEC.md` | — (reference only) | Evidence wiring: the two MCP servers, the Bash allowlist, build-log capture |
| `GATE_SPEC.md` | — (reference only) | Gate verdicts, the adversary, and the dossier |
| `CLOSURE_SPEC.md` | — (reference only) | Claim closure, observation sources, the `KERNEL_OBS` convention |
| `templates/` | — (copied per project) | `stage-state.template.yaml` — 11-line bootstrap for a new project |
| `skills/` | `~/.claude/skills/` | `gate-dossier` - assembles a gate readiness dossier |
| `agents/` | `~/.claude/agents/` | `gate-adversary` - read-only, refutes gate readiness |
| `hooks/` | referenced from `~/.claude/settings.json` | `SessionStart` digest, `PreToolUse` guards |
| `tools/` | invoked by hooks and skills | Cache generator, log fold, validators |

**Install scope: user level.** The framework is installed once at `~/.claude/` and applies to every ESP32 project. Project-specific state lives in each project's own `stage-state.yaml`.

---

## Multi-platform boundary

With STM32 and Raspberry Pi frameworks planned, the line between shared and platform-specific must be drawn before the second framework exists. Copying a shared document into three directories and letting the copies drift is the failure this project has already suffered twice (see I2 below).

| Concern | Shared across platforms | ESP32-specific |
|---|---|---|
| Stage model, gate checklists, precision bar | ✅ SECTION 1 | — |
| Assumption Register, trace records, task/issue/debt | ✅ SECTION 1, SECTION 4 | — |
| `stage-state.yaml` structure, attestation rules, log vocabulary, consistency rules | ✅ `STAGE_STATE_SCHEMA.md` | — |
| Evidence tiers E0–E3 and the claim-closure discipline | ✅ | — |
| Enforcement *levels* (`advisory` / `guard` / `strict`) and raise/lower rules | ✅ | — |
| The *guard list* at each level | — | ❌ the registry in `tools/guards.py` |
| Derived cache `ground_truth` fields | — | ❌ IDF version, target caps, `sdkconfig` |
| Digest `platform` block content | — | ❌ |
| Version-bound notes table | — | ❌ keyed by ESP-IDF major.minor |
| Evidence tooling | — | ❌ `idf.py mcp-server`, `idf.py size` / `coredump-info` |

**`STAGE_STATE_SCHEMA.md` is platform-independent** apart from §5.2 (the ESP-IDF v6.0 note) and the §9 cache example. When the STM32 or Raspberry Pi framework is created, **extract this document to a shared location and reference it — do not copy it.** The platform-specific parts become a per-platform adapter document alongside it.

## Design invariants

These are settled and not renegotiated. Anything in this directory that violates one is a defect.

| # | Invariant |
|---|---|
| **I1** | The engineer is the sole writer of `stage-state.yaml`. Agent writes are denied at `PreToolUse`. |
| **I2** | No fact that can go stale is stored by hand. What is stored is the means of obtaining it. |
| **I3** | The agent never states `PASS`. It reports `READY` / `NOT-READY` with evidence. A gate decision is a human utterance, recorded. |
| **I4** | Evidence is tiered E0–E3. A tier E3 claim is never an answer; it becomes an Assumption Register entry. |
| **I5** | No specification is duplicated. Gate checklists stay in SECTION 1; assumption detail stays in the Assumption Register; trace records stay in SECTION 4. |

### Consequence of I2 worth restating

This project has already produced two failures of exactly this kind, both caught only by cross-checking against ground truth:

- The specification pinned ESP-IDF v5.3 while the installed toolchain was v6.0.2 — a breaking-release gap that nothing detected.
- Three documents stated FreeRTOS stack depth in *words* for ESP32. ESP-IDF takes **bytes**; one of the three mandated the wrong convention in an audit criterion, which would have passed tasks undersized by 4×.

Neither was a reasoning failure. Both were stale facts written down confidently. Every design decision in this directory follows from that.

---

## Enforcement ladder

`enforcement` in `stage-state.yaml` is set by the engineer; the stage supplies its default.

| Level | Behaviour | Default at |
|---|---|---|
| `advisory` | Context injection and end-of-turn reminders only. No denial. | S1 |
| `guard` | Deterministic textual guards denied at `PreToolUse`, each overridable by the engineer. | S2, S3 |
| `strict` | `guard` plus evidence-path and numeric-claim checks, plus CI validation. | S4, S5 |

See `STAGE_STATE_SCHEMA.md` §5 for the guard list and the raise/lower rules.

---

## Platform baseline

| Property | Value |
|---|---|
| ESP-IDF | v6.0.2 |
| Toolchain | GCC 15.1.0 (Xtensa and RISC-V) |
| Primary targets | ESP32, ESP32-S3 |
| FreeRTOS | IDF FreeRTOS, SMP, based on vanilla v10.5.1 |
| Evidence tooling | `idf.py mcp-server` (Tools MCP), Espressif Documentation MCP, plus a narrow Bash allowlist for `size`, `size-components`, `coredump-info`, `monitor` |

This table records the *current* baseline for the framework's own calibration. Per I2, no component reads platform facts from this table at runtime — they are read from `sdkconfig`, `$IDF_PATH`, and `project://status`.

---

## Build order

| Phase | Contents | Status |
|---|---|---|
| 0 | `stage-state.yaml` schema; specification synchronised to ESP-IDF v6.0.2 | Done |
| 1 | `SessionStart` digest - `hooks/DIGEST_SPEC.md`, `tools/stage_kernel.py`, `hooks/session_start_digest.ps1` | Done |
| 2 | `PreToolUse` guards - `hooks/GUARD_SPEC.md`, `tools/guards.py`, `hooks/pre_tool_use_guard.ps1` | Done |
| 3 | MCP wiring + build-log evidence - `MCP_SPEC.md`, `tools/idf_run.ps1`, `tools/idf_mcp_launch.ps1` | Done |
| 4 | Gate validator, adversary subagent, dossier skill - `GATE_SPEC.md`, `tools/gates.py`, `agents/gate-adversary.md`, `skills/gate-dossier/` | Done |

**This is the whole agent.** Phases 0-4 implement human-in-the-loop support for SECTION 1: stage awareness, anti-hallucination guards, gate readiness with an adversary, and a decision boundary the agent cannot cross.

### Deliberately out of scope

A measurement-ingestion layer was built and then removed. It parsed `idf.py size` output and a custom runtime telemetry convention into a tier-E0 observation store, and was verified by running firmware under QEMU.

It worked, and it was the wrong thing to build. The goal here is an agent that supports an engineer's judgement, not a pipeline that collects embedded measurements. Two ideas from that work were worth keeping, and they now live in the documents that own them:

| Kept | Where |
|---|---|
| An assumption closed with `resolution: measured` must cite an evidence file that exists | `STAGE_STATE_SCHEMA.md` §10 |
| `numeric-claim` guard - a figure with a unit, stated as fact, with no citation | `hooks/GUARD_SPEC.md` §4 |

Both are anti-hallucination rules, which is why they belong. The parsers, the telemetry convention and the emulator work did not.

Build-log capture stays: `tools/idf_run.ps1` archives a build log, and the warning count from it is what makes the Gate 2→3 zero-warning criterion machine-checkable rather than a matter of opinion.

Gates 3→4 and above are unimplemented. Their criteria concern PCBs, pilot batches, burn-in and regulatory packages; writing checks against conditions never observed would produce specifications untested by reality.
