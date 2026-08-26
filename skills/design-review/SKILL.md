---
name: design-review
description: Run the SECTION 2 section 8 design phase review for an ESP32 project - machine verdicts over the 44-item checklist, adversary objections, and the list of criteria nothing can establish - and write it to tracking/pic-audit/dossiers/. Produces a READY / NOT-READY recommendation only; the outcome stays with the engineer.
argument-hint: [stage]
disable-model-invocation: true
allowed-tools: Bash(python ${CLAUDE_SKILL_DIR}/../../tools/stage_kernel.py *) Read Grep Glob
---

# Design phase review

Run the SECTION 2 §8 review for the project in the current working directory. `$1` optionally names the target stage; with no argument the stage comes from `stage-state.yaml`.

**Framework tools live at `${CLAUDE_SKILL_DIR}/../../tools/`.**

## This is not a gate

SECTION 1 gates sit *between* stages. §8 runs **inside** one, and its FAIL edge in the §1 DAG returns to the **Measurable Requirements Table** — not to the artifact that failed. Two consequences you must carry into the dossier:

1. The requirements table is re-edited once per review cycle. If it is not snapshotted, the *never renumbered / tombstone* invariant becomes unverifiable from the second cycle onward. Say so if no snapshot exists.
2. The outcome is recorded as `design_review_decided` with `outcome: ACCEPTED | REWORK` — never as `gate_decided`, and never by you.

## What this produces, and what it must never produce

A **recommendation**: `READY` or `NOT-READY`, with evidence.

It does not decide. It does not write `stage-state.yaml`. It does not emit the word that names a passing verdict. Those are the engineer's — see `README.md` invariants I1 and I3.

---

## Step 1 — Machine verdicts over the checklist

```bash
python ${CLAUDE_SKILL_DIR}/../../tools/stage_kernel.py design-review -C .
```

Four verdicts, same vocabulary as the gates:

| Verdict | Treatment |
|---|---|
| `MACHINE_CHECKED` | Established. Carry the evidence line across |
| `MACHINE_REFUTED` | **Established false.** Lead with it |
| `HUMAN_ATTESTED` | Covered by a valid attestation. Note who and when |
| `UNVERIFIABLE` | Neither — see step 3, this is the point |

If `anchors_lost` is reported, say so prominently: §8 wording changed and a criterion that used to be examined no longer is.

## Step 2 — Shape of the design artifacts

```bash
python ${CLAUDE_SKILL_DIR}/../../tools/stage_kernel.py design -C .
```

These establish **shape only**. A well-formed requirement can still be the wrong requirement, and the dossier must not blur that.

## Step 3 — The UNVERIFIABLE list is the product

Of the 44 items in §8, roughly 18 make a universal claim — *"every"*, *"all"*, *"each"* — over a set no file enumerates. You cannot establish *"every requirement drives a downstream artifact"* without a complete list of requirements.

**Do not treat this as a shortfall to be minimised.** A review that reports 41 unverifiable criteria has told the engineer exactly where their confidence has been resting on feeling rather than evidence. That is the most useful thing in the dossier.

For each UNVERIFIABLE criterion, say *what would have to exist* for it to become checkable. Usually a register pointer, an ID namespace, or a measurement record.

## Step 4 — Adversary

Spawn the `gate-adversary` subagent with the criteria list and the machine verdicts. Point it at the UNVERIFIABLE set — the others are already settled by evidence.

Also give it the `spec_defects` block from the digest. §8 reviews work built from a specification with **13 verified defects**, six of which are traps where copying the specification faithfully produces wrong output. An objection of the form *"this follows the spec example, and the spec example is wrong at line N"* is among the most valuable it can raise.

Carry its objections across **verbatim**, including the "could not check" list. Do not summarise away an objection you disagree with — the engineer disposes of objections, not you.

## Step 5 — Write the dossier

`tracking/pic-audit/dossiers/design-review-<YYYY-MM-DD>.md`:

```markdown
# Design Phase Review — SECTION 2 §8
**Project:** <id> · **Stage:** <S?> · **Date:** <YYYY-MM-DD>

## Recommendation
**<READY | NOT-READY>** — <one sentence, citing the counts>

This is a recommendation. The outcome is the engineer's, recorded as a
`design_review_decided` event in stage-state.yaml.

## Verdicts
machine-checked N · refuted N · attested N · unverifiable N (of 44)

### Refuted
| # | Criterion | Evidence |

### Unverifiable — and what would make each checkable
| # | Criterion | What is missing |

## Adversary objections
### OBJ-1 · <criterion>
**Severity:** blocking | material | minor
**Evidence:** <path:line or command output>
**Disposition:** _(blank — to be filled by the engineer)_

## Specification defects bearing on this review
<from the digest spec_defects block, if any artifact under review follows one>

## Requirements table snapshot
<path, or: NOT SNAPSHOTTED — the never-renumbered invariant is unverifiable
from the next review cycle onward>

## Objection tally
raised: N · accepted: — · rejected: —
```

Leave every `Disposition` blank and the tallies as `—`.

## Step 6 — Hand back

Report: the recommendation and counts, every `MACHINE_REFUTED` criterion with evidence, the unverifiable count with the two or three cheapest things that would reduce it, the objection count, and the dossier path.

Then state the next steps, which are the engineer's alone:

- disposition each objection in the dossier
- append a `design_review_decided` event with `outcome: ACCEPTED` or `REWORK`
- run `stage_kernel.py check -C .` to confirm the file is still consistent

Do not offer to perform any of them. Offering to record a decision on the engineer's behalf is where the boundary erodes first.
