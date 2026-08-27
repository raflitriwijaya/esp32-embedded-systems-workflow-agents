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
| `spec-defects.yaml` | — (data, read by the digest) | Verified defects in the workflow specification itself |
| `quality-attributes.yaml` | — (data, extracted) | Twelve-attribute vocabulary and the 44-edge conflict graph |
| `kconfig-migration.yaml` | — (data, extracted) | 721 ESP-IDF symbol renames and 4174 valid symbols, cut from the installed tree |
| `rsmr-matrix.yaml` | — (data, extracted) | SECTION 5 §7.1 matrix, 40 criteria × 5 stages, plus the debt ceiling |
| `RSMR_SPEC.md` | — (reference only) | Stage obligations, deferral validity, and the §7 Totals defect |
| `GATE_SPEC.md` | — (reference only) | Gate verdicts, the adversary, and the dossier |
| `CLOSURE_SPEC.md` | — (reference only) | Claim closure, observation sources, the `KERNEL_OBS` convention |
| `templates/` | — (copied per project) | `stage-state.template.yaml` — 11-line bootstrap for a new project |
| `skills/` | `~/.claude/skills/` | `gate-dossier` - gate readiness dossier · `design-review` - SECTION 2 sec.8 review |
| `agents/` | `~/.claude/agents/` | `gate-adversary` - read-only, refutes gate readiness |
| `hooks/` | referenced from `~/.claude/settings.json` | `SessionStart` digest, `PreToolUse` guards |
| `tools/` | invoked by hooks and skills | Cache generator, log fold, validators |

**Install scope: user level.** Installed once at `~/.claude/` and applies to every ESP32 project. Project-specific state lives in each project's own `stage-state.yaml`.

### Installing

Four **directory junctions**, so the installed framework and the repository never drift apart — a copy would let the two diverge silently, which is the failure this framework exists to prevent:

```powershell
New-Item -ItemType Junction "$env:USERPROFILE\.claude\tools"  -Target "<repo>\tools"
New-Item -ItemType Junction "$env:USERPROFILE\.claude\hooks"  -Target "<repo>\hooks"
New-Item -ItemType Junction "$env:USERPROFILE\.claude\agents" -Target "<repo>\agents"
New-Item -ItemType Junction "$env:USERPROFILE\.claude\skills\gate-dossier" -Target "<repo>\skills\gate-dossier"
```

Junctions need no elevation. Then register the two hooks and the spec path in `~/.claude/settings.json`:

```json
{ "hooks": {
    "SessionStart": [ { "hooks": [ { "type": "command",
      "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\.claude\hooks\session_start_digest.ps1\"" } ] } ],
    "PreToolUse":   [ { "matcher": "Write|Edit", "hooks": [ { "type": "command",
      "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\.claude\hooks\pre_tool_use_guard.ps1\"" } ] } ] },
  "env": { "EMBEDDED_WORKFLOW_SPEC_DIR": "<path to>\\workflow-iot" } }
```

`EMBEDDED_WORKFLOW_SPEC_DIR` matters because the gate validator parses criterion counts from `WORKFLOW_SECTION1.md` at run time rather than caching them. Without it the counts fall back to `null` — correct, but less useful.

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
| Toolchain | GCC 15.2.0 (Xtensa and RISC-V) — `tools.json` marks `esp-15.2.0_20251204` recommended for v6.0.2. SECTION 3 §2.2 and §6.2 both say 15.1.0; this line said so too until it was checked against the installed tree |
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
| 5 | RSMR x Stage obligations - `RSMR_SPEC.md`, `tools/rsmr.py`, `tools/extract_rsmr.py`, `rsmr-matrix.yaml` | Done |
| 6 | Framework self-test - `stage_kernel.py selftest`: guard registry vs `GUARD_SPEC.md`, legacy-header table vs the installed IDF, extracted copies vs their specification | Done |

**This is the whole agent.** Phases 0-4 implement human-in-the-loop support for SECTION 1: stage awareness, anti-hallucination guards, gate readiness with an adversary, and a decision boundary the agent cannot cross.

### Section 2 — the design phase

The same three layers, pointed at design artifacts instead of stage governance. No new machinery.

| Layer | Question | Section 1 | Section 2 |
|---|---|---|---|
| Context — digest | *Where does everything live?* | stage, RSMR bar, `not_known` | register pointers, `spec_defects`, design shape checks |
| Enforcement — guards | *What may be claimed?* | `.c` / `.h` sources | `.md` design documents |
| Review — validator | *Are we ready?* | Gate 1→2, 2→3 | SECTION 2 sec.8, 44 items |

**Context.** SECTION 7 sec.4.1 already fixes the repository layout; SECTION 2 mandates the artifacts but never references it. `current.registers` is the bridge, and every shape check is blocked without it. `tools/design_check.py` then establishes what shape can establish: ID format and uniqueness, the seven columns of sec.2.1, the five fields and nine bracket categories of sec.3.1, and referential integrity — a `REQ-` citation that resolves to nothing, a requirement no artifact references, an `ASM-` with no register entry.

`spec-defects.yaml` is the part with no precedent in Section 1. Guards check whether output is wrong; nothing checks whether output is **faithful to a defective example**. The specification writes `stack 4096 words` forty-three lines below its own rule that ESP-IDF takes bytes. An agent copying that is obeying, and is wrong. Thirteen defects were verified independently — computed where arithmetic was involved, checked against the installed ESP-IDF tree where platform reality was — and one further claim was investigated and **rejected as false**, which is why the register carries a `rejected:` section. The register is a curated fact list, exactly what invariant I2 warns against, so it stores the hash of the specification it was verified against and reports itself stale when that changes.

**Enforcement.** `numeric-claim` had four holes, each demonstrated by test before being closed: it skipped every line beginning with `|` — blinding it to the sec.2.1 Measurable Target column, the highest-risk surface in the chapter; it treated `REQ-` as a citation, though a requirement is what a number must satisfy rather than where it came from; it carried no electrical or mechanical units, so every hard number in the hardware bring-up checklist passed; and it denied only at `strict`, which is S4–S5, while the design phase runs at S1–S3.

The fix for the first hole is **not** to stop skipping tables. Design tables legitimately carry contract and target statements. Instead the guard parses the table and inspects only measurement columns — and treats the Assumption column as provenance, because sec.2.1 says it is. Testing against the specification's own worked example caught a cell-wise check flagging a correctly-filled row.

The exemptions must be preserved exactly. `Blocking: <= 10 ms` is a contract, not a measurement; a target, a budget, a limit and a range are intentions. A guard that fires on those fires on most of a well-written document, and gets switched off. **A guard's value is bounded by whether it stays on.**

**Review.** SECTION 2 sec.8 is not a gate: it runs *inside* a stage, and its FAIL edge returns to the Measurable Requirements Table rather than to the artifact that failed — so that table is re-edited each cycle, and the never-renumbered invariant depends on snapshotting it. The four verdicts, anchor-to-text discipline, adversary and dossier transfer unchanged; only a second anchor surface and a `design_review_decided` event are new.

Its most valuable output needs no checks at all: of 44 items, **18 make a universal claim** — *every*, *all*, *each* — over a set no file enumerates. A review reporting 41 unverifiable criteria has shown the engineer exactly where confidence rested on feeling. Two items are `MACHINE_REFUTED` today: both cite `CLAUDE.md sec.2` and `sec.5`, and that file has no numbered sections.

### Quality attributes — binding a requirement to what it buys

SECTION 1's stage bar is written in RSMR. SECTION 5 supplies measurable criteria for those four attributes and for no others. *Embedded IoT Engineering Quality Attributes — Cross-Platform Reference* defines **twelve**, with per-platform patterns and a declared relationship graph. SECTION 2's requirements table binds to none of them, so a `Measurable Target` is a number with no stated relationship to the quality it purchases or the method that would measure it.

An optional **Attribute** column closes that. `quality-attributes.yaml` is **extracted mechanically** from the reference by `tools/extract_attrs.py` — not paraphrased, because a graph an LLM restated drifts from its source with nobody noticing — and carries the source hash so a stale copy is detectable.

| Check | Establishes |
|---|---|
| `attribute-vocabulary` | Names come from the closed twelve |
| `attribute-measurable` | Flags requirements resting only on attributes nothing can measure |
| `attribute-conflicts` | The trade-offs this requirement set has bought into, quoted from the reference |
| `conflict-disposition` | Every trade-off carries an `AD-S<n>-<NNN>` naming both attributes — the decision, not just the conflict |
| `target-precision` | Each target is stated to the precision its stage demands (§2.2) |
| `target-binds-criterion` | Targets cite real SECTION 5 criteria; a fabricated id is caught on any row |

**Eight of the twelve have no measurable criteria anywhere.** Safety, Deterministic, Portable, Observable, Testable, Upgradeable, Secure and Resource Efficient carry definitions and patterns but no pass condition, so a requirement resting only on them is unverifiable by construction. Reporting that count is the same move as the 18-of-44 in the §8 review: naming what cannot be established is the product.

Conflicts report as `MACHINE_CHECKED`, not as failures. A project claiming both `Robust` and `Deterministic` is told *"defensive branches and retries widen WCET and add data-dependent execution paths"* — a cost already accepted, surfaced while the design can still change.

**Surfacing a trade-off is not the same as settling one.** `attribute-conflicts` reported the same conflicts every session, unchanged, forever: nothing separated a trade-off the engineer had weighed and settled from one they had never seen, and at a gate the reviewer was handed conflicts without decisions when it is the decisions that deserve review.

`conflict-disposition` closes that. A disposition is an `AD-S<n>-<NNN>` record naming both attributes — reusing the decision-record mechanism SECTION 2 §3.1 already defines and `decision-records` already validates, rather than inventing a second home for the same kind of fact (invariant I5). The check then quotes the decision back:

```
Deterministic vs Robust -> AD-S2-001-retry-bound: Robust wins over
Deterministic; I2C retries are capped at 3 with a fixed 5 ms backoff
```

A record that names both attributes only in *Alternatives considered* is reported as a **weak** disposition rather than accepted silently — an AD about CRC that happens to mention `Reliable` and `Resource Efficient` in a rejected option is not a decision about that trade-off, and a plain substring match would have swallowed it.

Stage-scaled: at S1 an undisposed conflict is surfaced, not demanded, because a Prototype is where trade-offs are still being discovered. From S2 it is `MACHINE_REFUTED`. What this establishes is that a decision exists and names the pair — never that the decision resolves the conflict.

The column is a **local convention**; SECTION 2 §2.1 does not mandate it. Without it the attribute checks report `UNVERIFIABLE` rather than failing. `templates/REQ-register.template.md` is the ready-to-copy table.

### Measurable target precision — the bar moves with the stage

SECTION 2 §2.2 opens with a sentence nothing enforced: *"A target that satisfies a gate at Stage 1 will fail the same gate at Stage 4."* `req-table-shape` confirmed the cell was non-empty and `target-binds-criterion` confirmed a cited criterion existed — so *"response shall be fast"* passed at Pre-Production exactly as `500 ms ± 100 ms @ −10…85 °C` did.

`target-precision` reads the `Measurable Target` cell against the stage's own standard:

| Stage | Bar | `500 ms ± 100 ms over −10…60 °C (calculated from datasheet)` |
|---|---|---|
| S1 | Order-of-magnitude accepted **if** logged as `ASM-S<n>-<NNN>` | ✅ |
| S2 | Units **and** tolerance | ✅ |
| S3 | Plus the operating conditions it holds under | ✅ |
| S4 | Plus an empirical reference — no bare calculated value | ❌ |

Identical cell, `MACHINE_CHECKED` at S3 and `MACHINE_REFUTED` at S4. Each finding quotes the stage's own violation response: *root-cause analysis required before gate* at S3, *gate blocked; waiver requires PIC risk acceptance* at S4.

**A target citing a SECTION 5 criterion is exempt from the numeric bar.** Many of those criteria are qualitative by design — `R-ESP-01`'s pass condition is *"Zero unchecked ESP-IDF return values in port code"* — and demanding a tolerance from them would refuse requirements that are already correctly formed. The citation carries the Check Method, which is what the bar is really after.

**A target with no operating conditions can say so.** A flash budget does not vary with temperature, and `condition-independent`, `build-time`, or `no operating-condition dependence` in the cell satisfies the S3 rule. Stating that none apply *is* an answer. Without this the check would nag forever at a target that is already right — the same "surfaced but never answerable" failure `conflict-disposition` exists to prevent.

What this establishes is how a target is **stated**. Whether the number is true is a different question, and no reading of the cell answers it.

### Section 5 — RSMR × Stage obligations

SECTION 5 §7.1 is the most quantified thing in the whole specification: 40 criteria against 5 stages, each cell `M`, `D`, or `N/A`. It is also the part an engineer cannot hold in their head — S3 alone makes **24 criteria Mandatory**.

`rsmr-matrix.yaml` is **extracted mechanically** by `tools/extract_rsmr.py`, together with the per-stage open-debt ceiling from SECTION 4 §5.4. Two checks run before it will write: the matrix must be monotonic (no criterion weakening as the stage advances — all 40 hold), and the parse must reproduce SECTION 5's own published Totals.

It does not. **The Totals table contradicts the matrix it summarises** at S2, S3 and S4, reporting zero N/A at S3 and S4 and redistributing exactly those rows into M and D. Recorded as `spec-defects.yaml` → `rsmr-totals`; the matrix governs. Trusting the summary would have made the agent demand four extra Mandatory criteria at Gate 3→4, and ask for fleet-scale evidence from a project with no fleet.

The scorecard is **generated from the matrix** rather than copied from §6.2, whose 22-row template — four of them placeholders — cannot express the 40 criteria the gate judges. *"Watchdog coverage verified"* stands for both *Watchdog implemented* and *Watchdog tested*, and cannot record a PASS on one with a FAIL on the other.

| Check | Establishes |
|---|---|
| `rsmr-scorecard-covers-stage` | Every M and D criterion has exactly one row; none duplicated |
| `rsmr-mandatory-verdicts` | No Mandatory criterion is FAIL, blank, malformed, or deferred |
| `rsmr-deferrals-valid` | Every unmet Deferrable cites a real DEBT that comes due at or before the stage it becomes Mandatory |
| `rsmr-evidence-reference` | Every PASS cites a locatable reference — §6.3 rule 4, *"test passed" is not evidence* |
| `debt-ceiling` · `debt-severity` · `debt-overdue` · `debt-acceptance` | SECTION 4 §5.3–5.5: count, severity, due date, and who may be accepted when |

**None of the 40 criteria is machine-checked, and the tool says so on every run.** *Thermal budget measured*, *Code reviewed by second engineer* — no static check settles those. What is mechanical is whether the assessment is complete and its deferrals valid, and that is the part worth automating:

```
RSMR-08: DEBT-002 revisits at S5, but this criterion becomes Mandatory at S4
         - the debt comes due after the gate it was meant to clear
```

`RSMR_SPEC.md` carries the full matrix, the defect analysis, and the check boundaries.

### Section 3 — migration, and configuration against the stage bar

ESP-IDF v6.0 is a breaking release, and SECTION 3 teaches v5 idiom in four of its own samples. The risk is not that the agent invents something — it is that the agent copies the specification faithfully and is wrong.

**The deprecation data is not curated.** A hand-written list of removed symbols is the stale-able fact invariant I2 warns against, and it would be wrong within one ESP-IDF release. ESP-IDF ships both halves already: 50 `sdkconfig.rename*` files mapping deprecated symbols to their replacements, and its Kconfig tree for what exists. `tools/extract_kconfig.py` turns them into `kconfig-migration.yaml` — **721 renames, 4174 symbols**, stamped with the IDF version so `selftest` catches an upgrade.

Building that extractor took three passes, each caught by checking rather than assuming:

| Assumed | Found |
|---|---|
| Symbols come from `config` declarations | 130 exist only via `select` — `ESP_COREDUMP_DATA_FORMAT_ELF` among them |
| Kconfig files are named `Kconfig`, `.projbuild`, `.in` | ESP-IDF uses arbitrary suffixes — `Kconfig.power`, `Kconfig.app_rollback`. 190 rename targets looked absent |
| Everything lives under `components/` | `COMPILER_ASSERT_NDEBUG_EVALUATE` and `COMPILER_DISABLE_GCC15_WARNINGS` are in the top-level `Kconfig` — both symbols the `warn-suppress` guard rests on |

Each miss would have flagged valid symbols as invented.

`kconfig-exists` now distinguishes three situations that used to read identically:

```
CONFIG_SW_COEXIST_ENABLE was renamed. ESP-IDF's own sdkconfig.rename map
  gives CONFIG_ESP_COEX_SW_COEXIST_ENABLE.
CONFIG_ESP32_TASK_WDT_TIMEOUT_S is not a Kconfig symbol in the installed
  v6.0.2 tree, and has no entry in its rename map.
CONFIG_ESP_COEX_SW_COEXIST_ENABLE is a real symbol but absent from every
  sdkconfig this project has — its component is most likely not in the build.
```

It also reads `sdkconfig` now, not just C sources: §2.2 requires sdkconfig to be committed, and a v5.x file carried forward holds symbols v6.0 removed. On the reference project's 1615 symbols it is silent.

**`assert-ndebug`** covers the one v6.0 system change the compiler cannot report. `CONFIG_COMPILER_ASSERT_NDEBUG_EVALUATE` now defaults to `n`, restoring standard C: the expression is not evaluated under `NDEBUG`. `assert(nvs_flash_init() == ESP_OK)` stops initialising in any release build, silently. The guard fires on a call inside the assertion and stays quiet on `assert(x > 0)`, `configASSERT(handle != NULL)` and `assert(sizeof(int) == 4)`.

The rest of §2.2's v6.0 list earns no guard, by this framework's founding rule — *do not guard what the compiler already reports*. `wifi_provisioning` is gone from the tree and the removed `mbedtls_*` primitives no longer link; both are loud failures.

### `config` — the stage bar for sdkconfig and partitions

`stage_kernel.py config` reads what §2.2 makes conditional on stage: assertions enabled from S3, bootloader log silence from S4, `ota_0`+`ota_1` from S3, warning suppression forbidden always. All of it is silent when wrong — a missing OTA partition is a build that flashes and can never update itself.

`config-symbols-real` runs first and checks the check: every symbol these rules name must exist in the installed tree. §2.2 asks for exactly that — *"Confirm both symbol names against the installed v6.0.2 Kconfig tree before wiring them into a CI check"* — and this is that CI check.

### Self-test — the framework checked against itself

`stage_kernel.py selftest` compares four hand-written artifacts against the code and the tree they describe. Every one of them had drifted at least once:

| Compared | Found |
|---|---|
| Guard registry vs `hooks/GUARD_SPEC.md` | `numeric-claim` documented as `strict`, registered as `guard` |
| `legacy-driver` replacements vs the installed IDF | `driver/mcpwm_prelude` missing its `.h` — the guard fired correctly and handed back a path that does not exist |
| `rsmr-matrix.yaml`, `quality-attributes.yaml` vs their specification hashes | (fresh) |
| `LOG_EVENT_FIELDS` vs `STAGE_STATE_SCHEMA.md` §7 | The table was documentation only — nothing read it |

Each check is verified by reintroducing the defect it was written for and confirming it fails. A test that can only pass proves nothing.

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
