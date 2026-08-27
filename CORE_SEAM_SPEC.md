# The Agnostic Core — Seam and Coverage

Two questions that turn out to be one. `stage_kernel.py core`.

---

## 1. The proof this rests on

SECTION 3 §3.1 states the rule in the header comment of its own example: the agnostic core *"does not include any platform header."* §4.1 says the core is the only thing a host compiler can test. §4.3 measures coverage on the core alone.

Whether ESP-IDF enforces that was verified rather than assumed. `esp_log.h` and `freertos/FreeRTOS.h` were put inside `components/core/` with `REQUIRES` empty, and the project built both ways:

| Build | Result |
|---|---|
| ESP32 firmware | `Project build complete` — not one word |
| Host tests, **the same source file** | `fatal error C1083: Cannot open include file: 'esp_log.h'` |

One file, two builds, opposite answers.

### 1.1 Why ESP-IDF cannot enforce it

```
common_component_reqs (13) — granted to every component without declaring anything:
  cxx, esp_libc, freertos, esp_hw_support, heap, log, soc, hal,
  esp_rom, esp_common, esp_system, esp_stdio, xtensa
```

`log` supplies `esp_log.h`. `freertos` supplies `FreeRTOS.h`. `soc` and `hal` supply the whole lower layer.

So **`REQUIRES: []` proves nothing**, and a check built on ESP-IDF's dependency graph alone would report a false clean. The graph is still worth reading — but for a different question, below.

### 1.2 Why the consequence is worse than it looks

Break the seam and the firmware keeps building. What stops is the **host** build — and with it §4.3's coverage bar, which has nothing left to measure. The failure is silent in exactly the direction that matters.

That is why this is a write-time guard rather than only a check: the build that catches it is the build an engineer may not run for days.

---

## 2. Four checks

| Check | Establishes | Cannot |
|---|---|---|
| `host-testable-core` | a core and a host test directory exist at the stage that requires them | that the tests are worth running |
| `core-purity` | no platform header, API, `CONFIG_` symbol or platform conditional in the core | that the core logic is right |
| `core-explicit-reqs` | no component dependency beyond the implicit 13 | purity — see §1.1 |
| `coverage-bar` | the measured figure against §4.3, on core files only | that the tests assert anything |

`core-explicit-reqs` reads `build_component_info` from `project_description.json`. Its value is narrow and real: it catches coupling added **deliberately**, which is the §2.2 `REQUIRES driver` trap recorded as spec defect `core-requires-driver`. `driver` is not one of the 13, so declaring it is an extra platform dependency on top of them — and the spec's comment *"no platform dependency"* is wrong twice.

### 2.1 The guard

`core-purity` at level **guard**: advisory at S1, denies at S2–S3. It fires on files under `current.registers.core` and stays silent everywhere else — the same `#include "esp_log.h"` in `main/` is correct code.

Dormant when `registers.core` is unset, and the digest says so rather than reporting silence as cleanliness.

**Host shims.** §4.2 puts `ports/host/inc` on the core's include path *"for host shims for FreeRTOS types"*. A core including a header the host shims still compiles both ways, so that finding says so instead of claiming the host build will break.

---

## 3. Coverage

### 3.1 §4.3 names tools that do not work here

§4.3 specifies **gcov/lcov**. Neither works with MSVC, and this workstation has no GCC or Clang. `Microsoft.CodeCoverage.Console.exe` from VS Build Tools 18 does, producing cobertura XML — machine-readable, per file, with `line-rate` and `branch-rate`, which are exactly the two figures §4.3 asks for.

Recorded rather than substituted silently. Two limits worth knowing: whether that tool is available on a CI runner has **not** been verified from here, and it is not what the specification names.

### 3.2 The core-only rule is not cosmetic

§4.3 measures the agnostic core alone. Including the test file inflates the figure — in the reference project, 99.3% overall against 100% for the core. The check reports how many non-core files it excluded.

### 3.3 A report over zero lines is not 100%

This one arrived by accident and is the reason the check exists rather than just running the tool. A host build failed, coverage collected nothing, and the tool emitted:

```xml
<coverage line-rate="1" branch-rate="1"><packages /></coverage>
```

**`line-rate="1"` — 100% — over zero files.** Anything reading the root attribute would report the bar met. The check refuses:

> *measures no line of the declared core, so the S3 bar is not met — it is unmeasured. A report over zero lines is not 100%*

Same vacuous-truth defect as an empty assumption log reading as no ambiguities, which this framework has already fixed once.

### 3.4 Binding

A coverage report older than the core sources describes code that has changed. It reports `UNVERIFIABLE` and names the files, the same rule the build log carries.

### 3.5 The bars

| Stage | Bar |
|---|---|
| S1 | none; one smoke test over the core API |
| S2 | ≥ 60% line |
| S3 | ≥ 80% line, every error-return path exercised |
| S4 | ≥ 90% line **and** ≥ 80% branch |
| S5 | ≥ 90% maintained, and must not decrease |

---

## 4. Declaring it

```yaml
current:
  registers:
    core:        components/core     # what the host build compiles
    host_tests:  tests/unit
    host_shims:  ports/host/inc      # optional
```

Source-tree pointers, not document registers. Every one is verified; an unresolvable path makes the dependent checks `UNVERIFIABLE` with the reason named, never a silent fallback.

---

## 5. What a clean run means

That the core carries no platform coupling, and that the measured coverage clears the stage bar.

Not that the core is correct. Not that the tests assert anything worth asserting. **Coverage counts lines reached, not claims checked** — a suite with no assertions still measures 100%, and the check says so on every passing run.
