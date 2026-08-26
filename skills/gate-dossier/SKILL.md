---
name: gate-dossier
description: Assemble a stage-gate readiness dossier for an ESP32 project - machine verdicts, adversary objections, and platform ground truth - and write it to gates/. Produces a READY / NOT-READY recommendation only; the gate decision stays with the engineer.
argument-hint: [gate]
disable-model-invocation: true
allowed-tools: Bash(python ${CLAUDE_SKILL_DIR}/../../tools/stage_kernel.py *) Read Grep Glob
---

# Gate dossier

Assemble the dossier for a stage gate. `$1` is an optional gate (`1->2`, `2->3`); with no argument the gate is derived from the project's current stage.

**Framework tools live at `${CLAUDE_SKILL_DIR}/../../tools/`.** The project is the current working directory.

## What this produces, and what it must never produce

It produces a **recommendation**: `READY` or `NOT-READY`, with evidence.

It does not decide. It does not write `stage-state.yaml`. It does not append a `gate_decided` event. It does not emit the word that names a passing verdict. Those are the engineer's, and the separation is the reason this framework exists — see `README.md` invariants I1 and I3.

If at any point you find yourself about to write a conclusion the evidence does not carry, stop and record the gap instead.

---

## Step 1 — Machine verdicts

```bash
python ${CLAUDE_SKILL_DIR}/../../tools/stage_kernel.py gate -C .
```

For a specific gate, set `STAGE_KERNEL_GATE` to `$1` first.

Read the output as-is. Each criterion carries one of four verdicts:

| Verdict | Treatment in the dossier |
|---|---|
| `MACHINE_CHECKED` | Established. Carry the evidence line across |
| `MACHINE_REFUTED` | **Established false.** This is the strongest finding available — lead with it |
| `HUMAN_ATTESTED` | Covered by an attestation. Note who and when |
| `UNVERIFIABLE` | Neither. Carry its hints across; these are what the adversary should attack |

If the validator reports `anchors_lost`, say so prominently: a check lost its anchor because SECTION 1 §3 wording changed, so a criterion that used to be examined no longer is.

## Step 2 — Platform ground truth

```bash
python ${CLAUDE_SKILL_DIR}/../../tools/stage_kernel.py digest -C .
```

Take from it: the installed ESP-IDF version and whether it matches the documented pin, per-target capabilities, and — most importantly — the `not_known` list. Anything named there is tier E3: it may not appear anywhere in the dossier as an established fact.

If `idf.match` is false, that is a dossier finding in its own right, regardless of gate.

## Step 3 — Adversary

Spawn the `gate-adversary` subagent with the gate, the machine verdicts, and the `not_known` list. Instruct it to concentrate on the `UNVERIFIABLE` criteria, since the others are already settled by evidence.

The subagent has no write tools. That is deliberate: it cannot manufacture the evidence it cites.

Carry its objections into the dossier **verbatim**, including its "could not check" list. Do not summarise away an objection you disagree with — the engineer disposes of objections, not you.

If it raises zero objections, record that as `raised: 0` and note it plainly. A run of zeros across reviews means the adversary is miscalibrated, not that the project is flawless.

## Step 4 — Write the dossier

Write to `gates/gate<from><to>-dossier-<YYYY-MM-DD>.md`, using the date from the machine output rather than a guessed one:

```markdown
# Gate <from>-><to> Readiness Dossier
**Project:** <id> · **Date:** <YYYY-MM-DD> · **Prepared by:** gate-dossier skill

## Recommendation
**<READY | NOT-READY>** — <one sentence, citing the counts>

This is a recommendation. The decision is the engineer's, recorded as a
`gate_decided` event in stage-state.yaml.

## Criterion verdicts
| # | Criterion | Verdict | Evidence |
|---|---|---|---|

## Adversary objections
### OBJ-1 · <criterion>
**Severity:** blocking | material | minor
**Evidence:** <path:line or command output>
**Why this refutes readiness:** …
**Disposition:** _(blank — to be filled by the engineer)_

## Could not check
- <criterion> — <what evidence would be needed>

## Platform ground truth at time of review
- ESP-IDF <installed> (documented pin <pinned>, match: <bool>)
- Targets: …
- Not known: …

## Objection tally
raised: N · accepted: — · rejected: —
```

Leave every `Disposition` blank and leave `accepted` / `rejected` as `—`. Filling them is the engineer's act, and an attestation citing this dossier is **invalid** until they add up (`STAGE_STATE_SCHEMA.md` §6.1, enforced by `stage_kernel.py check`).

## Step 5 — Hand back

Report to the engineer:

1. The recommendation and the counts
2. Every `MACHINE_REFUTED` criterion, with its evidence
3. The objection count
4. The dossier path
5. The exact next steps, which are theirs alone:
   - disposition each objection in the dossier
   - append an `attestation_made` event and an `attestations[]` entry if attesting
   - append a `gate_decided` event recording the decision
   - run `stage_kernel.py check -C .` to confirm the file is still consistent

Do not offer to perform steps 5b–5d. Offering to record a decision on the engineer's behalf is precisely the boundary this framework draws.
