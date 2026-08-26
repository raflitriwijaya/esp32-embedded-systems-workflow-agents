# GATE_SPEC — Gate Evaluation and Dossier (Phase 4)

**Validator:** `tools/gates.py` + `tools/stage_kernel.py gate` · **Adversary:** `agents/gate-adversary.md`
**Output location:** `gates/gate<from><to>-dossier-<YYYY-MM-DD>.md`

---

## 1. What phase 4 changed

The digest had reported `machine_checked: null` and `unverifiable: null` since phase 1, with a note that the validator did not exist. It exists now:

```yaml
next_gate:
  gate: "2->3"
  criteria: { total: 9, machine_checked: 0, machine_refuted: 1,
              human_attested: 0, unverifiable: 8 }
  recommendation: NOT-READY   # 1 criterion(s) refuted by evidence
```

`null` never became `0`. The distinction was the point of reporting null in the first place: zero means nothing passed, null means nothing was examined.

---

## 2. Four verdicts, not two

| Verdict | Meaning |
|---|---|
| `MACHINE_CHECKED` | A check **established** the criterion |
| `MACHINE_REFUTED` | A check **established it false** |
| `HUMAN_ATTESTED` | A valid attestation covers it (SCHEMA §6.1) |
| `UNVERIFIABLE` | Neither. Not a failure — a statement about what is known |

`MACHINE_REFUTED` is the most valuable of the four and the reason a two-valued scheme is not enough. A dossier that can only report readiness is an advocate. One that can report *"criterion 8 is false, here is the log line"* is a check.

`UNVERIFIABLE` being first-class is what keeps the validator honest. An evaluator with only pass/fail available invents a verdict to fill the row.

---

## 3. The rule that keeps the check set small

> A criterion is `MACHINE_CHECKED` only when the check **establishes** it — not when it establishes something adjacent.

`design/icd/` containing files does not establish *"an ICD exists for every protocol link with typed field definitions and valid-range constraints."* Treating it as though it did would manufacture confidence, which is worse than admitting ignorance.

Weak signals are not discarded. They attach to an `UNVERIFIABLE` criterion as **hints**, where they inform a human decision instead of impersonating one.

Applied honestly, this yields few checks — and that number is itself information:

| Gate | Criteria | Machine-checkable today |
|---|---|---|
| 1→2 | 6 | 2 |
| 2→3 | 9 | 2 |

Two of nine at Gate 2→3 means seven rest on human judgement. Knowing that is more useful than a dashboard reporting 9/9 on the strength of file-existence tests.

---

## 4. Implemented checks

| id | Gate | Establishes | Refutes when |
|---|---|---|---|
| `platform-conventions` | 1→2 | No Arduino constructs, no drivers removed in v6.0, FreeRTOS task creation present | Any violation found in source |
| `assumptions-owned` | 1→2 | Every open assumption carries a deadline | Any open assumption without one |
| `zero-warnings` | 2→3 | Zero warnings across every configured target | An archived build log shows warnings |
| `no-tbd` | 2→3 | No literal `TBD` in connectivity/ICD documents | `TBD` found — reported with `path:line` |

`zero-warnings` is the strongest: an archived build log settles it in either direction. Absent a log the answer is `UNVERIFIABLE`, never zero — this is the same discipline that keeps `build_warnings` in `not_known` until a log exists.

`no-tbd` carries a hint acknowledging its own limit: absence of the literal `TBD` proves only that nothing is *openly marked* outstanding.

---

## 5. Anchoring — checks bind to text, not to position

Each check declares a regex that must match the criterion text in SECTION 1 §3. Consequences:

- Reordering the checklist cannot mis-assign a check.
- Rewording a criterion past its anchor degrades it to `UNVERIFIABLE` and the validator reports the orphaned check by name (`anchors_lost` in the digest).

A check silently applying to the wrong criterion is worse than no check, because it produces a confident wrong verdict rather than a visible gap.

---

## 6. The adversary

`agents/gate-adversary.md` is a read-only subagent (`disallowedTools: Write, Edit`, `permissionMode: plan`) whose task is to **refute** readiness.

Its rules matter more than its prompt:

1. Never outputs `PASS` / `FAIL` / `ready`.
2. Every objection anchors to a quoted criterion. Unanchored objections are out of scope.
3. Every objection cites `path:line`, a command and its output, or a named absent file.
4. **It cannot create the evidence it cites** — the write tools are withheld structurally, not by instruction. This closes the self-fabricated-evidence loop, which is convincing in a transcript and worthless in fact.
5. It must say what it could not check.

The `Disposition` field in its output is deliberately left blank for the engineer.

### Why solo makes this mandatory rather than optional

SECTION 6 assumes a PIC distinct from the author. Where they are the same person, the second pair of eyes is gone and self-review biases toward passing. The adversary is the nearest available substitute — but it is a substitute, not an equivalent: it can be confidently wrong, and the engineer is the only one who can catch that.

### Calibration signal

`objections: {raised, accepted, rejected}` is recorded in each attestation. A run of `raised: 0` means the adversary is miscalibrated, not that the project is flawless. The counts are logged precisely so that pattern becomes visible over time — a signal that does not exist without the log.

---

## 7. Dossier

Assembled from three parts and written to `gates/gate<from><to>-dossier-<YYYY-MM-DD>.md`:

| Part | Source |
|---|---|
| Machine verdicts, per criterion, with evidence | `stage_kernel.py gate` |
| Objections with severity, evidence, and blank dispositions | `gate-adversary` subagent |
| Platform ground truth and stated unknowns | `stage_kernel.py digest` |

The dossier ends with `READY` or `NOT-READY` and never with `PASS`. Recording the decision is a separate, human act: a `gate_decided` event appended to `stage-state.yaml`, carrying `decision`, `by`, `unmet[]`, and the dossier path.

### What makes an attestation on this dossier valid

Per SCHEMA §6.1, all four must hold:

```
[ ] dossier: points to a file that exists
[ ] the dossier lists the adversary's objections explicitly
[ ] every objection has a disposition: accepted (-> gap, TASK, or ASM) or
    rejected with a written reason
[ ] objections.accepted + objections.rejected == objections.raised
```

Conditions 1 and 4 are machine-checked by `stage_kernel.py check`; a violation raises the digest's `INCONSISTENT` state and the stage stops being asserted.

---

## 8. Running it

```
python tools/stage_kernel.py gate -C <project>              # next gate for the current stage
STAGE_KERNEL_GATE="1->2" python tools/stage_kernel.py gate -C <project>   # a specific gate
```

Orchestration is the `/gate-dossier` skill (`skills/gate-dossier/SKILL.md`):

| Step | Action |
|---|---|
| 1 | `stage_kernel.py gate` — machine verdicts |
| 2 | `stage_kernel.py digest` — platform ground truth and the `not_known` list |
| 3 | Spawn `gate-adversary`, pointed at the `UNVERIFIABLE` criteria |
| 4 | Write `gates/gate<from><to>-dossier-<YYYY-MM-DD>.md` |
| 5 | Hand back — recommendation, refutations, objection count, dossier path |

It is `disable-model-invocation: true`: it writes a file and drives a review, so the engineer controls when it runs. It is deliberately **not** forked — the orchestration stays in the main session where the engineer can see it, and the heavy SECTION 1 reading happens inside the validator rather than in context.

Objections are carried across **verbatim**, including ones the assembler disagrees with. Dispositions are left blank. The skill's final instruction is explicit that it must not offer to record the decision on the engineer's behalf: offering is where the human-in-the-loop boundary erodes first.

---

## 9. Not covered

| Gap | Why |
|---|---|
| Gates 3→4, 4→5, 5→1 | Their criteria need PCBs, pilot batches, burn-in and regulatory packages. Writing checks against conditions never yet observed would produce specifications untested by reality |
| Trace-chain integrity (SECTION 4 §1.4) | Requires the trace register, which no project has yet |
| Per-criterion machine checks beyond the four listed | Deliberate. Each additional check must earn `MACHINE_CHECKED` honestly, or it belongs as a hint |
