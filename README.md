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
| `templates/` | — (copied per project) | `stage-state.template.yaml` — 11-line bootstrap for a new project |
| `skills/` | `~/.claude/skills/` | User-invoked and model-invoked skills |
| `agents/` | `~/.claude/agents/` | Subagent definitions (auditor, adversary) |
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

| Phase | Contents | Requires hardware |
|---|---|---|
| 0 | `stage-state.yaml` schema; specification synchronised to v6.0.2 | No |
| 1 | `SessionStart` digest: bootstrap-aware, multi-target — `hooks/DIGEST_SPEC.md`, `tools/stage_kernel.py`, `hooks/session_start_digest.ps1` | No |
| 2 | `PreToolUse` guards - `hooks/GUARD_SPEC.md`, `tools/guards.py`, `hooks/pre_tool_use_guard.ps1` | No |
| 3 | MCP wiring + build-log evidence - `MCP_SPEC.md`, `tools/idf_run.ps1`, `tools/idf_mcp_launch.ps1` | No, except flashing |
| 4 | Gate dossier skill and adversary subagent, calibrated for gates 1→2 and 2→3 | No |
| 5 | Numeric claim closure loop and numeric guards | **Yes** |
| 6 | Gates 3→4 and above | Yes |

Phases 0-3 are complete. `espressif-docs` is registered at user scope and awaits a one-time OAuth login via `/mcp`; the ESP-IDF Tools server is registered per project, since `idf.py mcp-server` only runs inside one. Phase 4 is buildable before the first ESP32 project exists, on a throwaway project created with `idf.py create-project` and `idf.py set-target`.
