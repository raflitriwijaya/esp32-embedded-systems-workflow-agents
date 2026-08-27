# Measurable Requirements Register

Format: SECTION 2 §2.1, plus an **Attribute** column that section does not mandate.

Copy to `design/requirements/REQ-register.md` and point `current.registers.requirements`
at it in `stage-state.yaml`.

---

## Why the extra column

SECTION 2 §2.1 defines seven columns, and `Measurable Target` among them holds a
number. Nothing in the specification connects that number to the quality it is
meant to buy, or to the method that would measure it. Two consequences follow:

- A measured `±0.5 °C` and an invented `±0.5 °C` are byte-identical and pass
  every mechanical check.
- Trade-offs stay invisible. A requirement demanding both fault tolerance and
  bounded worst-case timing has bought a conflict, and nothing says so.

The `Attribute` column closes both. It draws on a **closed vocabulary of twelve**
from *Embedded IoT Engineering Quality Attributes — Cross-Platform Reference*,
and the framework holds that vocabulary plus the reference's 44 declared
conflict edges in `quality-attributes.yaml`.

**This is a local convention. The specification does not require it**, and the
checks say so: with no `Attribute` column the attribute checks report
`UNVERIFIABLE`, never a failure.

---

## The twelve

| # | Attribute | Measurable criteria in SECTION 5? |
|---|---|---|
| 1 | Robust | ✅ `R-ESP-01…09` |
| 2 | Scalable | ✅ `S-ESP-01…06` |
| 3 | Maintainable | ✅ `M-ESP-01…07` |
| 4 | Reliable | ✅ `RL-ESP-01…08` |
| 5 | Safety | ❌ none anywhere |
| 6 | Deterministic | ❌ none anywhere |
| 7 | Portable | ❌ none anywhere |
| 8 | Observable | ❌ none anywhere |
| 9 | Testable | ❌ none anywhere |
| 10 | Upgradeable | ❌ none anywhere |
| 11 | Secure | ❌ none anywhere |
| 12 | Resource Efficient | ❌ none anywhere |

Only the first four are RSMR, and SECTION 5 measures those alone. **A requirement
resting only on attributes 5–12 is unverifiable by construction.** That is not a
reason to avoid claiming them — it is a reason to know you have, before a gate
tells you.

---

## Table

| ID | Requirement | Attribute | Measurable Target | Drives | Source | Assumption | Stage Gate |
|---|---|---|---|---|---|---|---|
| REQ-S2-001 | The node shall continue operating through I2C bus faults | Robust | per R-ESP-04: every fallible call has a defined failure branch; fault injected in TEST-004 | FW-001 | BA-001 | NONE | Gate 2→3 |
| REQ-S2-002 | The node shall run 30 days without intervention | Reliable | per RL-ESP-06: stack high-water-mark margin ≥ 25% on every task | FW-002 | BA-002 | ASM-S2-001 | Gate 2→3 |
| REQ-S2-003 | *(delete this row and the two above; they are examples)* | | | | | | |

### Column rules

| Column | Rule |
|---|---|
| **ID** | `REQ-S<n>-<NNN>`, sequential within stage, **never renumbered** — a deleted ID leaves a tombstone row |
| **Requirement** | One declarative, falsifiable sentence. No conjunctions — split compound statements |
| **Attribute** | One or more of the twelve, comma-separated. Closed vocabulary |
| **Measurable Target** | A quantified threshold. Where the attribute is one of the four RSMR ones, **cite the SECTION 5 criterion** (`R-ESP-04`, `RL-ESP-06`, …) so the number carries its measurement method |
| **Drives** | `HW-`, `FW-`, `TEST-`, `SCH-` ids |
| **Source** | `BA-`, `INC-`, `AD-`, or a parent `REQ-` |
| **Assumption** | `ASM-S<n>-<NNN>` or `NONE`. Where the target rests on an unvalidated premise, this column is the provenance — the numeric-claim guard treats it as such |
| **Stage Gate** | The gate at which this requirement is verified |

---

## What the checks establish

Run `python tools/stage_kernel.py design -C .`:

| Check | Establishes |
|---|---|
| `attribute-vocabulary` | Every name is one of the twelve. Nothing invented |
| `attribute-measurable` | Every requirement names at least one attribute, and flags those resting only on attributes nothing can measure |
| `attribute-conflicts` | The declared trade-offs this requirement set has bought into, quoted from the reference |
| `target-binds-criterion` | Targets cite real SECTION 5 criteria; a fabricated criterion id is caught on any row |
| `conflict-disposition` | Every trade-off carries a decision record naming both attributes |

None of them establishes that a target is **correct**. That boundary does not move.

---

## Conflicts are not defects — but they are questions

`attribute-conflicts` reports `MACHINE_CHECKED` even when it finds conflicts, because a
conflict is a cost already accepted rather than a fault. A project claiming both
`Robust` and `Deterministic` gets:

> Deterministic vs Robust: defensive branches and retries widen WCET and add
> data-dependent execution paths.

Knowing that while the design can still change is the point. Discovering it at a
72-hour run is not.

Reporting it forever is not the point either. `conflict-disposition` asks for the
answer: an `AD-S<n>-<NNN>` decision record naming **both** attributes, under
`current.registers.decisions`. Nothing new to learn — it is the same five-field
record SECTION 2 §3.1 already defines, and `decision-records` already checks.

```markdown
# AD-S2-001 - bounded retry policy

Decision: Robust wins over Deterministic; I2C retries are capped at 3 with a
fixed 5 ms backoff so the worst-case path stays bounded.
Driven by: REQ-S2-001, REQ-S2-002
Technical reason: [timing constraint] an unbounded retry loop makes worst-case
response time data-dependent, which Deterministic forbids. Capping at 3 keeps
the fault branch while holding WCET at 515 ms.
Alternatives considered: unbounded retry (rejected: unbounded WCET); no retry
(rejected: single bus glitch drops the sample).
Stage impact: S2
```

Once recorded, the gate sees the decision rather than the conflict:

```
Deterministic vs Robust -> AD-S2-001-retry-bound: Robust wins over
Deterministic; I2C retries are capped at 3 with a fixed 5 ms backoff
```

Two things worth knowing before you write one:

**Name both attributes where you argue, not only where you reject.** A record
naming both only inside *Alternatives considered* is reported as a **weak**
disposition. An AD about CRC that mentions `Reliable` and `Resource Efficient`
in a rejected option is not a decision about that trade-off, and the check
declines to pretend it is.

**At S1 this is surfaced, not demanded.** A Prototype is where trade-offs are
still being found. From S2 an undisposed conflict is `MACHINE_REFUTED`.

The check establishes that a decision exists and names the pair. Whether the
decision is *right* is yours, and stays yours.
