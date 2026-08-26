# GUARD_SPEC — Layer 3 PreToolUse Guards

**Implementation:** `../tools/guards.py` + `../tools/stage_kernel.py guard` · **Hook:** `pre_tool_use_guard.ps1`
**Registry is authoritative.** No other file may list the guards; the digest imports the list.

---

## 1. The principle that keeps this list short

> Do not guard what the compiler already reports. A guard earns its cost only where the toolchain is silent.

A guard costs three things every time it runs: latency on a write, a chance of a false denial, and the erosion of trust that follows one. Against that, catching a mistake the compiler would have caught a second later buys nothing.

Ranking the candidates by whether the toolchain is silent produced this set — and cancelled one that had already been designed:

| Guard | Does the compiler say anything? | Verdict |
|---|---|---|
| `stack-unit` | **Silent.** Compiles cleanly, allocates 4× wrong | Build — highest value |
| `kconfig-exists` | **Silent.** A typo'd `CONFIG_` evaluates false and disables the feature | Build — highest value |
| `core-pin` | **Silent** until it fails on the device | Build |
| `warn-suppress` | **Silent.** Fakes a Gate 2→3 criterion | Build |
| `legacy-driver` | Loud for removed headers, **silent for `driver/i2c.h`** (EOL, still compiles) | Build — for the EOL case and the replacement hint |
| `arduino-ban` | Loud, but SECTION3 §3.3 mandates the check | Build — cheap |
| ~~`header-exists`~~ | **Loud and unambiguous** | **Cancelled** — pure false-positive surface |

`stack-unit` is first on evidence, not on theory: this project stated the wrong unit in three of its own specification documents, and one of them mandated the wrong convention as an audit criterion.

---

## 2. Severity is a function of enforcement, not of the guard

Each guard declares a level. The project's `enforcement` decides what happens to a finding.

| `enforcement` | guard-level findings | strict-level findings |
|---|---|---|
| `advisory` | surfaced as `additionalContext` | surfaced as `additionalContext` |
| `guard` | **`permissionDecision: deny`** | surfaced as context |
| `strict` | **deny** | **deny** |

This keeps the ladder's promise that `advisory` never blocks, while still giving a Stage 1 project the warning. The checks always run; only the consequence changes.

A denial is overridable through the normal permission flow. The reason text says so explicitly: *an override is a decision you are recording, not a bypass.*

---

## 3. Registry

| id | level | applies to | implemented |
|---|---|---|---|
| `stack-unit` | guard | C/C++ sources | ✅ |
| `kconfig-exists` | guard | C/C++ sources | ✅ |
| `core-pin` | guard | C/C++ sources | ✅ |
| `warn-suppress` | guard | `sdkconfig*` | ✅ |
| `legacy-driver` | guard | C/C++ sources | ✅ |
| `arduino-ban` | guard | C/C++ sources | ✅ |
| `numeric-claim` | strict | markdown under `design/`, `hardware/`, `reliability/`, `gates/` | ✅ |
| `evidence-path` | strict | — | ❌ not built |

`stage_kernel.py` imports `guards.implemented_guards()` for the digest's `active_guards`, so an unimplemented guard can never be advertised as active. This is invariant I2 applied to the framework itself.

---

## 4. What each guard catches

### `stack-unit`
- `sizeof(StackType_t)` in a file that calls `xTaskCreate*` — the vanilla-FreeRTOS word-count idiom
- A `#define …STACK… ` whose trailing comment says *words*
- An `xTaskCreate*` call annotated *words*

Cites SECTION5 RL-ESP-06 and SECTION2 §7.1.

### `kconfig-exists`
Every `CONFIG_[A-Z0-9_]+` in the text is checked against the union of symbols in the project's known `sdkconfig` files. Symbols the file `#define`s itself are excluded. A symbol absent from every sdkconfig is reported — the canonical case being `CONFIG_ESP32_TASK_WDT_TIMEOUT_S` (a v4-era name) against `CONFIG_ESP_TASK_WDT_TIMEOUT_S`, where the watchdog is simply never configured.

Symbols are read **live from `sdkconfig`**, not from `.stage-cache.json`: a cache lagging a `menuconfig` run by one session would produce false denials, and false denials are what get a guard layer switched off.

If no sdkconfig is reachable the guard returns nothing. It does not guess.

### `core-pin`
Parses the final argument of `xTaskCreatePinnedToCore(...)`. Reports core ≥ 2 always, and core ≥ 1 when any configured target has `CONFIG_FREERTOS_UNICORE=y`. A non-literal argument (`tskNO_AFFINITY`, a variable) is not decidable and is left alone.

### `warn-suppress`
Fires on `sdkconfig*` files containing `CONFIG_COMPILER_DISABLE_DEFAULT_ERRORS=y` or `CONFIG_COMPILER_DISABLE_GCC15_WARNINGS=y`. Enabling either satisfies the Gate 2→3 zero-warning criterion by suppression rather than by fixing code — a gate finding, not a build fix.

### `legacy-driver`
The nine headers removed in ESP-IDF v6.0, each mapped to its replacement, plus `driver/i2c.h` (EOL, removal in v7.0) and a few unmistakable legacy call names. When the installed IDF is a 5.x release the message says *deprecated rather than removed — it will break on upgrade*, because the installed version is read from ground truth rather than assumed.

### `numeric-claim`

Strict level. Fires on a figure with a unit stated as fact, with nothing showing where it came from.

| Fires | Allowed |
|---|---|
| `The sensor task peak stack usage is 3100 bytes.` | `Peak stack 3100 bytes (tests/reports/hwm-2026-08-27.log).` |
| `Measured RX sensitivity is -94 dBm at the far point.` | `Assume 4096 bytes for now - ASM-S2-001, due 2026-09-30.` |
| `Sleep current settles at 12 uA.` | `Target: sleep current must be below 20 uA.` |

The exemptions carry the design. A **target**, a **budget**, a **limit** and a **range** are intentions, not measurements; flagging them would train the engineer to dismiss the guard, and a dismissed guard protects nothing. Headings, table rows and templates are excluded, as is `tests/reports/`, which holds the evidence itself.

This is the textual counterpart of the closure rule in `STAGE_STATE_SCHEMA.md` §10: a measurement that never happened reads exactly like one that did, and the difference is a citation. Verified 14/14 across signal and false-positive cases.

### `arduino-ban`
`Arduino.h`, `pinMode(`, `digitalWrite(`, `digitalRead(`, `analogWrite(`. Redundant with the compiler, retained because CLAUDE.md §2 and SECTION3 §3.3 state the rule.

---

## 5. Scoping — where false positives were actually found

Source guards apply only to `.c .h .cpp .hpp .cc .cxx .ino`, and skip any path containing the **segments** `build`, `managed_components`, `.git`, `third_party`, `node_modules`, `dist`, or a segment beginning `build_`, `espressif__`, `cmake-build`.

Segments, not substrings. The first implementation tested for the substring `/build/` and therefore failed to skip the relative path `build/x/gen.c` — generated code would have tripped `legacy-driver` on every write. Two false positives were found this way during testing; both are now in the test set below.

Comments are blanked before code scans, so a legacy call mentioned in prose does not fire.

---

## 6. Failure behaviour

| Situation | Behaviour |
|---|---|
| A guard raises | That guard yields a finding saying it raised, and that the file was **not checked** — silence is never reported as cleanliness |
| The guard runner raises | `additionalContext` states the file was NOT checked; the write is allowed |
| No `stage-state.yaml` | Enforcement defaults to `advisory` — warn, never deny |
| Not an ESP-IDF project, or `.no-stage-governance` | No output |
| PowerShell wrapper fails | Exits 0, diagnostic to stderr; guards **fail open** |

Failing open is deliberate. A guard layer that can block all work when it breaks will be removed after the first incident; one that goes quiet and says so can be repaired.

---

## 7. Hook input field names

The `tool_input` key names differ between the tool schemas (`file_path`, `content`, `old_string`/`new_string`) and the hooks documentation summary (`path`, `file_text`). Rather than trust either, the guard probes `file_path`/`path`/`notebook_path` for the path and `content`/`file_text`/`new_string`/`new_source` plus an `edits[]` array for the text.

A guard that silently never fires is worse than no guard, and would be indistinguishable from a clean codebase.

---

## 8. Test set

```
[ ] stack-unit: sizeof(StackType_t) in an xTaskCreate file  -> deny
[ ] stack-unit: #define …STACK… with a "words" comment      -> deny
[ ] kconfig-exists: CONFIG_ESP32_TASK_WDT_TIMEOUT_S         -> deny
[ ] kconfig-exists: symbol #defined in the same file        -> allow
[ ] core-pin: core 2                                        -> deny
[ ] core-pin: core 1 on a dual-core project                 -> allow
[ ] core-pin: tskNO_AFFINITY                                -> allow
[ ] warn-suppress: sdkconfig with DISABLE_DEFAULT_ERRORS=y  -> deny
[ ] legacy-driver: driver/adc.h, driver/i2c.h, adc1_get_raw -> deny
[ ] legacy-driver: the same name inside a // comment        -> allow
[ ] arduino-ban: Arduino.h, pinMode(                        -> deny
[ ] clean idiomatic v6 source                               -> allow
[ ] Edit tool payload via new_string                        -> deny
[ ] build/x/gen.c, build_esp32s3/y.c, managed_components/   -> allow
[ ] README.md                                               -> allow
[ ] non-ESP-IDF project directory                           -> allow
[ ] enforcement advisory: findings surfaced, nothing denied -> allow
[ ] digest active_guards contains no unimplemented guard
[ ] numeric-claim: bare measurement fires; target/budget/range/citation do not
```

Driving a guard manually:

```
echo '{"tool_name":"Write","cwd":"<proj>","tool_input":{"file_path":"main/a.c","content":"..."}}' \
  | python tools/stage_kernel.py guard
```

---

## 9. Install

```json
{ "hooks": { "PreToolUse": [ { "matcher": "Write|Edit",
  "hooks": [ { "type": "command",
    "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\pre_tool_use_guard.ps1\"" } ] } ] } }
```

The wrapper rejects payloads that mention neither a source extension nor `sdkconfig` before starting Python, so an unrelated write costs one substring test.
