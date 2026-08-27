"""Extract the RSMR x Stage mandatory matrix and the debt ceiling, mechanically.

SECTION5 sec.7.1 states 40 criteria against 5 stages as M / D / N-A, and SECTION4
sec.5.4 states an open-debt ceiling per stage. Both are already quantified; the
work here is only to move them into a form a check can read without a human
retyping 200 cells.

Hand-transcription is what this file exists to avoid. A mistyped cell would make
the agent demand the wrong thing at a gate, and nothing downstream could tell.
So the matrix is parsed, then verified against the spec's own Totals table -
SECTION5 supplies its own checksum, which is unusual and worth using.

Regenerate rather than editing the output; source_sha256 makes a stale copy
detectable, and stage_kernel.py selftest re-runs the parse.
"""
import hashlib
import pathlib
import re
import sys

WI = pathlib.Path("c:/Users/maschdev3/Documents/workflow-embedded-systems/workflow-iot")
S5 = WI / "WORKFLOW_SECTION5.md"
S4 = WI / "WORKFLOW_SECTION4.md"
OUT = pathlib.Path(__file__).resolve().parent.parent / "rsmr-matrix.yaml"

STAGES = ["S1", "S2", "S3", "S4", "S5"]
DIM = {"ROBUST": "Robust", "SCALABLE": "Scalable",
       "MAINTAINABLE": "Maintainable", "RELIABLE": "Reliable"}


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def norm(v):
    v = v.replace("*", "").strip().upper()
    if v in ("N/A", "NA", "N-A"):
        return "NA"
    return v if v in ("M", "D") else None


def parse_matrix(text):
    """[(dimension, area, {stage: M|D|NA})] in document order."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("### 7.1"))
    rows, dim = [], None
    for l in lines[start:]:
        if l.startswith("**Totals") or l.startswith("### 7.2"):
            break
        if not l.startswith("|"):
            continue
        c = cells(l)
        if len(c) != 7 or set(c[2]) <= set("-: "):
            continue
        if c[0].lower().startswith("rsmr"):
            continue
        d = c[0].replace("*", "").strip()
        if d:
            if d.upper() not in DIM:
                continue
            dim = DIM[d.upper()]
        if dim is None or not c[1]:
            continue
        marks = [norm(x) for x in c[2:7]]
        if any(m is None for m in marks):
            continue
        rows.append((dim, c[1], dict(zip(STAGES, marks))))
    return rows


def parse_totals(text):
    """{stage: (M, D, NA, total)} as SECTION5 declares them."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("**Totals"))
    out = {}
    for l in lines[start:start + 12]:
        if not l.startswith("|"):
            continue
        c = cells(l)
        if len(c) != 5:
            continue
        m = re.match(r"Stage\s+(\d)", c[0].replace("*", "").strip())
        if not m:
            continue
        try:
            out["S" + m.group(1)] = tuple(int(x) for x in c[1:5])
        except ValueError:
            continue
    return out


def parse_debt_limits(text):
    """{stage: {max_open, rule}} from SECTION4 sec.5.4."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("### 5.4"))
    names = {"prototype": "S1", "functional prototype": "S2",
             "pre-production": "S3", "production-ready": "S4",
             "field-deployed maintenance": "S5"}
    out = {}
    for l in lines[start:start + 20]:
        if not l.startswith("|"):
            continue
        c = cells(l)
        if len(c) != 4:
            continue
        st = names.get(c[0].replace("*", "").strip().lower())
        if not st:
            continue
        m = re.search(r"(?:\u2264|<=)\s*(\d+)\s*open", c[2])
        out[st] = {"max_open": int(m.group(1)) if m else None, "rule": c[2]}
    return out


def esc(s):
    s = " ".join(str(s).split())
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# The sec.7 Totals table disagrees with the sec.7.1 matrix it summarises, at
# Stages 2, 3 and 4. Verified and recorded as spec-defects.yaml `rsmr-totals`:
# the matrix is monotonic and self-consistent, while the summary reports zero
# N/A at Stages 3 and 4 and redistributes exactly those rows into M and D.
#
# Pinning the known divergence here rather than dropping the comparison keeps
# the checksum working. Any OTHER mismatch still refuses to write - which is the
# whole reason SECTION5 supplying its own totals is worth using.
KNOWN_TOTALS_DEFECT = {
    "S2": ((5, 10, 25, 40), (5, 16, 19, 40)),
    "S3": ((24, 8, 8, 40), (28, 12, 0, 40)),
    "S4": ((34, 2, 4, 40), (38, 2, 0, 40)),
}
RANK = {"NA": 0, "D": 1, "M": 2}


def main():
    t5, t4 = S5.read_text(encoding="utf-8"), S4.read_text(encoding="utf-8")
    rows = parse_matrix(t5)
    declared = parse_totals(t5)
    debt = parse_debt_limits(t4)

    # --- a criterion must never weaken as the stage advances -----------------
    # This is the matrix's real invariant, and it is what establishes that the
    # matrix - not the Totals table - is the sound artifact of the two.
    slipped = []
    for _dim, area, marks in rows:
        r = [RANK[marks[s]] for s in STAGES]
        if any(r[i + 1] < r[i] for i in range(4)):
            slipped.append("{}: {}".format(
                area, " ".join(s + "=" + marks[s] for s in STAGES)))
    if slipped:
        print("MATRIX IS NOT MONOTONIC - a criterion weakens as the stage "
              "advances. Nothing written:")
        for x in slipped:
            print("  - " + x)
        return 1

    # --- verify the parse against the spec's own totals ----------------------
    problems, known = [], []
    for st in STAGES:
        got = (sum(1 for _, _, m in rows if m[st] == "M"),
               sum(1 for _, _, m in rows if m[st] == "D"),
               sum(1 for _, _, m in rows if m[st] == "NA"),
               len(rows))
        want = declared.get(st)
        if want is None:
            problems.append(st + ": SECTION5 declares no total to check against")
        elif got == want:
            continue
        elif KNOWN_TOTALS_DEFECT.get(st) == (got, want):
            known.append("{}: matrix {} vs Totals table {}".format(st, got, want))
        else:
            problems.append("{}: parsed M/D/NA/total {}, SECTION5 declares {}"
                            .format(st, got, want))
    if problems:
        print("PARSE DOES NOT MATCH THE SPEC'S OWN TOTALS, and the mismatch is "
              "not the recorded one - nothing written:")
        for p in problems:
            print("  - " + p)
        print("\nEither the parse is wrong, or SECTION5 changed. Re-verify "
              "before touching KNOWN_TOTALS_DEFECT.")
        return 1

    def becomes_m(marks):
        return next((s for s in STAGES if marks[s] == "M"), None)

    L = ["# RSMR x Stage mandatory matrix, and the open-debt ceiling per stage.",
         "#",
         "# EXTRACTED MECHANICALLY. Regenerate with tools/extract_rsmr.py rather",
         "# than editing: 200 hand-typed cells is 200 chances to make the agent",
         "# demand the wrong thing at a gate, with nothing able to notice.",
         "#",
         "# The parse is verified against SECTION5's own Totals table before this",
         "# file is written, so a misparse cannot ship silently.",
         "#",
         "# M  - must pass for the gate to clear; may not be deferred",
         "# D  - may fail if logged as DEBT-xxx with revisit_stage <= becomes_mandatory",
         "# NA - not applicable at that stage; no assessment required",
         "",
         "source: workflow-iot/" + S5.name,
         "source_sha256: " + hashlib.sha256(S5.read_bytes()).hexdigest(),
         "debt_source: workflow-iot/" + S4.name,
         "debt_source_sha256: " + hashlib.sha256(S4.read_bytes()).hexdigest(),
         "spec: SECTION5 sec.7.1 (matrix), sec.7.3 (deferral rules); "
         "SECTION4 sec.5.4 (debt ceiling)",
         "",
         "# Counted from the sec.7.1 matrix itself. Where SECTION5's own Totals",
         "# table disagrees, the matrix governs - see spec-defects.yaml",
         "# `rsmr-totals`. sec.7.2 and sec.7.3 both operate on individual cells.",
         "totals:"]
    for st in STAGES:
        m = sum(1 for _, _, x in rows if x[st] == "M")
        d = sum(1 for _, _, x in rows if x[st] == "D")
        na = sum(1 for _, _, x in rows if x[st] == "NA")
        note = ""
        if st in dict.fromkeys(k for k in KNOWN_TOTALS_DEFECT) and \
                declared.get(st) != (m, d, na, len(rows)):
            w = declared[st]
            note = ("   # SECTION5's Totals table says M={}, D={}, N/A={} "
                    "- defective, see rsmr-totals".format(w[0], w[1], w[2]))
        L.append("  {}: {{ mandatory: {}, deferrable: {}, not_applicable: {}, "
                 "total: {} }}{}".format(st, m, d, na, len(rows), note))
    L += ["", "debt_limits:"]
    for st in STAGES:
        e = debt.get(st, {})
        cap = e.get("max_open")
        L.append("  " + st + ":")
        L.append("    max_open: " + (str(cap) if cap is not None
                                     else "null   # no ceiling at this stage"))
        L.append("    rule: " + esc(e.get("rule", "")))
    L += ["", "criteria:   # {} rows, document order preserved".format(len(rows))]
    for i, (dim, area, marks) in enumerate(rows, 1):
        bm = becomes_m(marks)
        L.append("  - id: RSMR-{:02d}".format(i))
        L.append("    dimension: " + dim)
        L.append("    area: " + esc(area))
        L.append("    becomes_mandatory: " + (bm if bm else
                                              "null   # never mandatory"))
        L.append("    stages: { "
                 + ", ".join(s + ": " + marks[s] for s in STAGES) + " }")
    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")

    import yaml
    d = yaml.safe_load(OUT.read_text(encoding="utf-8"))
    print("wrote " + OUT.name)
    print("  criteria: {}".format(len(d["criteria"])))
    print("  matrix is monotonic: no criterion weakens as the stage advances")
    if known:
        print("  SECTION5's own Totals table disagrees at {} stage(s) - recorded "
              "defect `rsmr-totals`; the matrix governs:".format(len(known)))
        for x in known:
            print("    " + x)
    else:
        print("  parse matches SECTION5's own Totals table at all 5 stages")
    for st in STAGES:
        t = d["totals"][st]
        cap = d["debt_limits"][st]["max_open"]
        print("    {}: M={:2}  D={:2}  N/A={:2}   max_open_debt={}"
              .format(st, t["mandatory"], t["deferrable"], t["not_applicable"],
                      cap if cap is not None else "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
