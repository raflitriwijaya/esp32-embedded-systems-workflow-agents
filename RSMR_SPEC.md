# RSMR × Stage Obligations

What SECTION 5 §7 asks of a project at its current stage, and whether the record answers it.

---

## 1. The boundary, stated first

Of the 40 criteria in SECTION 5 §7.1, this module machine-checks **none**.

*Thermal budget measured (not calculated)*. *Code reviewed by second engineer*. *MTBF target demonstrated*. No static check settles any of them, and a tool that claimed otherwise would be the failure this framework exists to prevent.

What is fully mechanical is the bookkeeping around them — and that turns out to be the part an engineer genuinely cannot hold in their head:

- which criteria are Mandatory right now, and so may not be deferred
- which are Deferrable, and by which stage each falls due
- whether every Mandatory criterion has a recorded verdict **at all**
- whether each deferral is backed by a `DEBT-xxx` that comes due early enough to be worth anything
- whether open debt is within the stage ceiling, at permitted severities

Every finding says which of the two it is. `rsmr-mandatory-verdicts` reporting `MACHINE_CHECKED` means *the record is complete*, never *the criteria are met*.

---

## 2. The matrix

`rsmr-matrix.yaml`, extracted mechanically by `tools/extract_rsmr.py`. 200 hand-typed cells is 200 chances to make the agent demand the wrong thing at a gate with nothing able to notice.

| Stage | Mandatory | Deferrable | N/A |
|---|---|---|---|
| S1 Prototype | 0 | 3 | 37 |
| S2 Functional Prototype | 5 | 10 | 25 |
| S3 Pre-Production | 24 | 8 | 8 |
| S4 Production-Ready | 34 | 2 | 4 |
| S5 Field-Deployed | 40 | 0 | 0 |

Two things verify the extraction before it is written:

**Monotonicity.** No criterion may weaken as the stage advances (`N/A < D < M`). All 40 rows hold. This is the matrix's real invariant.

**The spec's own Totals table.** SECTION 5 §7 publishes its own M/D/N-A counts — an unusual gift, and worth using. The extractor refuses to write when the parse disagrees.

### 2.1 Where the spec disagrees with itself

The Totals table matches at S1 and S5 and **disagrees at S2, S3 and S4**. Recorded as `spec-defects.yaml` → `rsmr-totals`; the matrix governs.

| Stage | Matrix (counted) | §7 Totals table |
|---|---|---|
| S2 | 5 M / 10 D / 25 N-A | 5 M / **16** D / **19** N-A |
| S3 | 24 M / 8 D / 8 N-A | **28** M / **12** D / **0** N-A |
| S4 | 34 M / 2 D / 4 N-A | **38** M / 2 D / **0** N-A |

Three findings put the fault in the summary rather than the matrix. The matrix is monotonic throughout. The Totals table reports **zero N/A at both S3 and S4**, and the surplus it hands to M and D is exactly the 8 and 4 rows the matrix marks N/A there. And those rows are self-evidently inapplicable early — *Fleet MTBF vs. predicted comparison* cannot be assessed on a pre-production unit. The summary appears to have been computed as though N/A ceased to exist past Stage 2.

Consequence had it been trusted: the agent would demand four more Mandatory criteria at Gate 3→4 than the matrix requires, and would ask for fleet-scale evidence from a project with no fleet.

`KNOWN_TOTALS_DEFECT` in the extractor pins this exact divergence. **Any other mismatch still refuses to write** — dropping the comparison would have thrown away the checksum.

---

## 3. The scorecard is generated, not filled in from §6.2

SECTION 5 §6.2 supplies a scorecard template of 22 rows, four of them `[Platform-specific criteria]` placeholders, against a §7.1 matrix of 40. §7.2 step 3 nonetheless instructs the reviewer to check *"every Mandatory criterion against the scorecard (§6.2)"*.

Several Mandatory criteria have no row there to check against — fault-injection testing, HardFault/panic handler, CHANGELOG updated per change, thermal budget measured. Two more collapse a pair of matrix rows into one line: *"R-02: Watchdog coverage verified"* stands for both **Watchdog implemented** and **Watchdog tested (induced hang → reset)**, and cannot express a PASS on one with a FAIL on the other.

`stage_kernel.py rsmr-scorecard` generates from the matrix instead, so every criterion the gate judges has exactly one line to answer it. The §6.2 header fields are kept, so the artifact is still the one §6.3 describes.

It refuses to overwrite an existing scorecard. Those verdicts are the engineer's own assessment, and no tool of mine discards them.

---

## 4. Checks

| check | establishes | cannot establish |
|---|---|---|
| `rsmr-scorecard-covers-stage` | every M and D criterion has exactly one row; none duplicated | that any row is answered honestly |
| `rsmr-mandatory-verdicts` | no M criterion is FAIL, blank, malformed, or carrying a DEBT | that a PASS is true |
| `rsmr-deferrals-valid` | every unmet D cites a real DEBT whose `revisit_stage` ≤ the stage it becomes Mandatory | that the debt will actually be paid |
| `rsmr-evidence-reference` | every PASS cites a locatable file, line, commit, or record id | that the reference says what the row claims |
| `debt-ceiling` | open count within the SECTION 4 §5.4 stage ceiling | that the items are correctly severity-rated |
| `debt-severity` | S4 admits zero open debt at severity ≥ S3; S5 zero S1/S2 | — |
| `debt-overdue` | no open item past its `revisit_date`, and none undated (§5.5) | — |
| `debt-acceptance` | S4 severity accepted only from S3, S3 only from S4, S1/S2 never (§5.3) | that the PIC sign-off happened |

### 4.1 The deferral rule is the one worth having

§7.3 permits an unmet Deferrable criterion, but only against a debt item whose `revisit_stage` is at or before the stage where the criterion becomes Mandatory. A deferral that comes due *after* the gate it was meant to clear is not a deferral — it is a gap with paperwork.

Across 40 criteria and a debt folder, this is not something anyone spots by eye:

```
RSMR-08: DEBT-002 revisits at S5, but this criterion becomes Mandatory at S4
         - the debt comes due after the gate it was meant to clear
```

---

## 5. Wiring

```yaml
current:
  registers:
    rsmr_scorecard: quality/rsmr-scorecard-S3.md
    debt: tracking/debt          # default when unset
```

```
stage_kernel.py rsmr-scorecard    # generate this stage's scorecard
stage_kernel.py rsmr              # check the record against the matrix
stage_kernel.py selftest          # confirm the matrix is still fresh
```

The digest carries the counts and any refutation at session start, because *24 Mandatory criteria at S3* is exactly the kind of fact that costs nothing to state and everything to forget.

Debt records are read per SECTION 4 §5.2: `tracking/debt/DEBT-*.md`, fields `id`, `severity`, `status`, `revisit_stage`, `revisit_date`. Items are open unless `status` says otherwise.

---

## 6. What a clean run means

`stage_kernel.py rsmr` reporting eight `MACHINE_CHECKED` results means the engineer's assessment is **complete, internally consistent, and validly deferred**.

It does not mean the project is ready for its gate. It means the record no longer hides whether it is.
