#!/usr/bin/env python3
"""ESP32 Stage Kernel - RSMR x Stage obligations, and the debt that defers them.

WHAT THIS CAN AND CANNOT ESTABLISH

Of the 40 criteria in SECTION5 sec.7.1, this module machine-checks none. "Thermal
budget measured (not calculated)", "Code reviewed by second engineer", "MTBF
target demonstrated" - no static check settles any of them, and pretending
otherwise would be the exact failure the framework exists to prevent.

What is fully mechanical is the bookkeeping around them, and that turns out to be
the part an engineer actually cannot hold in their head:

  - which criteria are Mandatory at the current stage (M may not be deferred)
  - which are Deferrable, and by which stage each must be resolved
  - whether every Mandatory criterion has a recorded verdict at all
  - whether each deferral is backed by a DEBT item whose revisit_stage is early
    enough to be worth anything
  - whether the open debt is within the stage ceiling, at permitted severities

So the verdicts here are about the completeness and validity of the engineer's
assessment, never about whether a criterion is genuinely met. Every finding says
which of the two it is.

FORMATS
  SECTION5 sec.7.1  the matrix, via rsmr-matrix.yaml (extract_rsmr.py)
  SECTION5 sec.6.3  scorecard usage rules - PASS/FAIL/N-A, evidence is a
                    reference and not a narration, every FAIL links to a
                    TASK-xxx or DEBT-xxx
  SECTION4 sec.5.2  debt record schema
  SECTION4 sec.5.4  open-debt ceiling per stage
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

VERIFIED = "MACHINE_CHECKED"
REFUTED = "MACHINE_REFUTED"
SKIPPED = "UNVERIFIABLE"

MATRIX_FILE = "rsmr-matrix.yaml"
STAGES = ["S1", "S2", "S3", "S4", "S5"]

SCORECARD_COLUMNS = ["id", "criterion", "verdict", "evidence", "debt"]
VERDICTS = {"PASS", "FAIL", "N-A"}
RE_RSMR_ID = re.compile(r"^RSMR-\d{2}$")
RE_DEBT_ID = re.compile(r"\bDEBT-\d{3,}\b")
RE_TASK_ID = re.compile(r"\bTASK-\d{3,}\b")

# SECTION5 sec.6.3 rule 4: "Test passed" is not evidence;
# `tests/reports/test-042.log:45-78 - ...` is. A reference points at a place.
RE_EVIDENCE_REF = re.compile(
    r"[\w./\\-]+\.(?:c|h|cpp|py|md|log|txt|yaml|yml|json|csv|png|pdf)\b"
    r"|:\d+(?:-\d+)?\b"
    r"|\b[0-9a-f]{7,40}\b"                     # a commit
    r"|\b(?:TASK|DEBT|REQ|AD|TEST|INC)-[\w-]+\b")

# SECTION4 sec.5.3: OPEN -> ACCEPTED is permitted only for S4 severity at
# Stage 3+, and S3 severity at Stage 4+ with PIC sign-off. S1/S2 debt may
# never be accepted.
ACCEPT_RULES = {"S4": "S3", "S3": "S4"}


def _f(check, status, why, evidence=None, hints=None):
    return {"check": check, "status": status, "why": why,
            "evidence": evidence or [], "hints": hints or []}


def _stage_rank(s):
    try:
        return STAGES.index(str(s).strip().upper())
    except ValueError:
        return None


# ------------------------------------------------------------------- loading

def load_matrix():
    f = Path(__file__).resolve().parent.parent / MATRIX_FILE
    if not f.is_file():
        return None
    try:
        import yaml
        return yaml.safe_load(f.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return None


def _tables(text, want):
    """[(header, [(line_no, cells)])] for EVERY table carrying `want` columns.

    Reading only the first one would have been a quiet disaster: the scorecard
    splits Mandatory and Deferrable into separate tables, so the deferral check
    would have reported a clean result over rows it never read. Caught by giving
    the test a scorecard with known-bad deferrals and getting MACHINE_CHECKED
    back.
    """
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        l = lines[i]
        if not l.strip().startswith("|"):
            i += 1
            continue
        head = [c.strip().lower() for c in l.strip().strip("|").split("|")]
        if not all(w in head for w in want) or i + 1 >= len(lines) or \
                set(lines[i + 1].replace("|", "").strip()) - set("-: ") != set():
            i += 1
            continue
        rows, j = [], i + 2
        while j < len(lines) and lines[j].strip().startswith("|"):
            rows.append((j + 1, [c.strip()
                                 for c in lines[j].strip().strip("|").split("|")]))
            j += 1
        out.append((head, rows))
        i = j
    return out


def load_scorecard(root, reg):
    """[(line, id, criterion, verdict, evidence, debt)] or None."""
    p = reg.get("rsmr_scorecard")
    if not p:
        return None, "current.registers.rsmr_scorecard is not set"
    f = root / str(p)
    if not f.is_file():
        return None, f"{p} does not exist"
    tables = _tables(f.read_text(encoding="utf-8", errors="ignore"),
                     ["id", "verdict"])
    if not tables:
        return None, f"{p} has no table with ID and Verdict columns"
    out, seen = [], {}
    for head, rows in tables:
        idx = {c: head.index(c) for c in SCORECARD_COLUMNS if c in head}
        for ln, cells in rows:
            def cell(name, _cells=cells, _idx=idx):
                i = _idx.get(name)
                return _cells[i] if i is not None and i < len(_cells) else ""
            rid = cell("id").strip().strip("`")
            if not RE_RSMR_ID.match(rid):
                continue
            if rid in seen:
                # Two rows for one criterion means two verdicts, and nothing
                # says which governs. Surfaced rather than silently last-wins.
                out.append({"line": ln, "id": rid, "criterion": cell("criterion"),
                            "verdict": "DUPLICATE", "evidence": "",
                            "debt": "", "duplicate_of": seen[rid]})
                continue
            seen[rid] = ln
            out.append({"line": ln, "id": rid, "criterion": cell("criterion"),
                        "verdict": cell("verdict").strip().upper().replace("/", "-"),
                        "evidence": cell("evidence"), "debt": cell("debt")})
    return out, None


def load_debt(root, reg):
    """[{id, severity, status, revisit_stage, revisit_date, path, line}]."""
    p = reg.get("debt") or "tracking/debt"
    d = root / str(p)
    if not d.is_dir():
        return None, f"{p} is not a directory"
    items = []
    for f in sorted(d.glob("DEBT-*.md")):
        text = f.read_text(encoding="utf-8", errors="ignore")

        def field(name):
            m = re.search(rf"^\s*{name}\s*:\s*(.+?)\s*$", text, re.M)
            if not m:
                return None
            v = m.group(1).strip().strip('"').strip("'")
            return None if v.lower() in ("null", "none", "") else v

        items.append({
            "id": field("id") or f.stem,
            "severity": (field("severity") or "").upper() or None,
            "status": (field("status") or "").lower() or None,
            "revisit_stage": (field("revisit_stage") or "").upper() or None,
            "revisit_date": field("revisit_date"),
            "source_stage": (field("source_stage") or "").upper() or None,
            "path": f.relative_to(root).as_posix(),
        })
    return items, None


# -------------------------------------------------------------------- checks

def c_scorecard_covers_stage(ctx):
    """Every criterion the stage actually asks for has a row to answer it.

    A blank row and a missing row look identical from a distance, and only one
    of them is a decision. This separates them.
    """
    mx, sc, stage = ctx["matrix"], ctx["scorecard"], ctx["stage"]
    if sc is None:
        return _f("rsmr-scorecard-covers-stage", SKIPPED, ctx["scorecard_why"])
    due = [c for c in mx["criteria"] if c["stages"][stage] in ("M", "D")]
    dupes = [r for r in sc if r["verdict"] == "DUPLICATE"]
    have = {r["id"] for r in sc if r["verdict"] != "DUPLICATE"}
    missing = [c for c in due if c["id"] not in have]
    stray = sorted(have - {c["id"] for c in due})
    na = len(mx["criteria"]) - len(due)
    if missing or dupes:
        ev = [f"{c['id']} [{c['stages'][stage]}] {c['area']} - no row"
              for c in missing[:8]]
        ev += [f"{r['id']} line {r['line']}: a second row, the first is at line "
               f"{r['duplicate_of']} - two verdicts, and nothing says which governs"
               for r in dupes[:4]]
        return _f("rsmr-scorecard-covers-stage", REFUTED,
                  f"the scorecard does not answer {stage} exactly: "
                  f"{len(missing)} of {len(due)} required criteria have no row, "
                  f"{len(dupes)} criterion(s) have more than one",
                  ev,
                  [f"{na} further criteria are N/A at {stage} and need no row"]
                  + ([f"{len(stray)} row(s) answer criteria not required at "
                      f"{stage}: {', '.join(stray[:6])} - harmless, but they are "
                      f"not what this gate reviews"] if stray else []))
    return _f("rsmr-scorecard-covers-stage", VERIFIED,
              f"all {len(due)} criteria required at {stage} have a scorecard row "
              f"({na} more are N/A at this stage)")


def c_mandatory_verdicts(ctx):
    """SECTION5 sec.7.3: a Mandatory criterion may not be deferred. It must PASS."""
    mx, sc, stage = ctx["matrix"], ctx["scorecard"], ctx["stage"]
    if sc is None:
        return _f("rsmr-mandatory-verdicts", SKIPPED, ctx["scorecard_why"])
    by = {r["id"]: r for r in sc}
    mand = [c for c in mx["criteria"] if c["stages"][stage] == "M"]
    blank, failed, deferred, bad = [], [], [], []
    for c in mand:
        r = by.get(c["id"])
        if not r:
            continue                                   # covered by the check above
        v = r["verdict"]
        if not v or v == "_____":
            blank.append(f"{c['id']} line {r['line']}: no verdict recorded - "
                         f"{c['area']}")
        elif v == "FAIL":
            failed.append(f"{c['id']} line {r['line']}: FAIL - {c['area']}")
        elif v not in VERDICTS:
            bad.append(f"{c['id']} line {r['line']}: verdict {v!r} is not one of "
                       f"PASS / FAIL / N-A")
        if RE_DEBT_ID.search(r["debt"]) and v != "PASS":
            deferred.append(f"{c['id']} line {r['line']}: carries "
                            f"{RE_DEBT_ID.search(r['debt']).group(0)}, but this "
                            f"criterion is Mandatory at {stage} and may not be "
                            f"deferred (sec.7.3)")
    problems = deferred + failed + blank + bad
    if problems:
        return _f("rsmr-mandatory-verdicts", REFUTED,
                  f"{len(problems)} of {len(mand)} Mandatory criteria at {stage} "
                  f"do not stand: {len(deferred)} deferred, {len(failed)} FAIL, "
                  f"{len(blank)} unanswered, {len(bad)} malformed",
                  problems[:10],
                  ["a FAIL on a Mandatory criterion blocks the gate (sec.6.1); "
                   "this reports the scorecard's own state, not whether the "
                   "criterion is truly met"])
    return _f("rsmr-mandatory-verdicts", VERIFIED,
              f"all {len(mand)} Mandatory criteria at {stage} are recorded PASS "
              f"or N-A, none deferred - the record is complete, which is not the "
              f"same as the criteria being met")


def c_deferrals_valid(ctx):
    """A deferral is only worth something if it comes due before it must be met.

    sec.7.3: an unmet Deferrable criterion needs a DEBT item with
    revisit_stage <= the stage at which it becomes Mandatory.
    """
    mx, sc, stage = ctx["matrix"], ctx["scorecard"], ctx["stage"]
    debt = ctx["debt"]
    if sc is None:
        return _f("rsmr-deferrals-valid", SKIPPED, ctx["scorecard_why"])
    by = {r["id"]: r for r in sc}
    dl = {d["id"]: d for d in (debt or [])}
    defr = [c for c in mx["criteria"] if c["stages"][stage] == "D"]
    unmet, bad = [], []
    for c in defr:
        r = by.get(c["id"])
        if not r or r["verdict"] == "PASS":
            continue
        if r["verdict"] == "N-A":
            continue
        ref = RE_DEBT_ID.search(r["debt"] or "") or RE_DEBT_ID.search(
            r["evidence"] or "")
        if not ref:
            unmet.append(f"{c['id']} line {r['line']}: {r['verdict'] or 'no '
                         'verdict'} with no DEBT item - {c['area']}")
            continue
        did = ref.group(0)
        if debt is None:
            continue                                   # register unreadable
        item = dl.get(did)
        if not item:
            bad.append(f"{c['id']} line {r['line']}: cites {did}, which is not "
                       f"in the debt register")
            continue
        rs, bm = _stage_rank(item["revisit_stage"]), _stage_rank(
            c["becomes_mandatory"])
        if item["revisit_stage"] is None:
            bad.append(f"{c['id']}: {did} sets no revisit_stage, so the deferral "
                       f"has no deadline")
        elif rs is None:
            bad.append(f"{c['id']}: {did} has revisit_stage "
                       f"{item['revisit_stage']!r}, which is not a stage")
        elif bm is not None and rs > bm:
            bad.append(f"{c['id']}: {did} revisits at {item['revisit_stage']}, "
                       f"but this criterion becomes Mandatory at "
                       f"{c['becomes_mandatory']} - the debt comes due after the "
                       f"gate it was meant to clear")
    problems = unmet + bad
    if problems:
        return _f("rsmr-deferrals-valid", REFUTED,
                  f"{len(problems)} deferral(s) at {stage} are not valid: "
                  f"{len(unmet)} unmet with no debt item, {len(bad)} with a debt "
                  f"item that does not cover them",
                  problems[:10])
    n = sum(1 for c in defr if (by.get(c["id"]) or {}).get("verdict") not in
            ("PASS", "N-A", None))
    return _f("rsmr-deferrals-valid", VERIFIED,
              f"{len(defr)} criteria are Deferrable at {stage}; the {n} not met "
              f"each carry a DEBT item that comes due at or before the stage "
              f"where they become Mandatory")


def c_evidence_is_reference(ctx):
    """sec.6.3 rule 4: evidence is a reference, not a narration."""
    sc, stage = ctx["scorecard"], ctx["stage"]
    if sc is None:
        return _f("rsmr-evidence-reference", SKIPPED, ctx["scorecard_why"])
    passing = [r for r in sc if r["verdict"] == "PASS"]
    if not passing:
        return _f("rsmr-evidence-reference", SKIPPED,
                  "no PASS rows yet - nothing claims evidence")
    weak = [f"{r['id']} line {r['line']}: {r['evidence'][:60]!r}"
            for r in passing
            if not RE_EVIDENCE_REF.search(r["evidence"] or "")]
    if weak:
        return _f("rsmr-evidence-reference", REFUTED,
                  f"{len(weak)} of {len(passing)} PASS rows cite no locatable "
                  f"reference - 'test passed' is not evidence (sec.6.3 rule 4)",
                  weak[:10],
                  ["a reference names a file, line, commit, or record id; this "
                   "checks that one is present, never that it says what the row "
                   "claims"])
    return _f("rsmr-evidence-reference", VERIFIED,
              f"all {len(passing)} PASS rows cite a locatable reference - present, "
              f"which is not the same as checked")


def c_debt_ceiling(ctx):
    """SECTION4 sec.5.4: an open-debt ceiling per stage."""
    mx, debt, stage = ctx["matrix"], ctx["debt"], ctx["stage"]
    if debt is None:
        return _f("debt-ceiling", SKIPPED, ctx["debt_why"])
    lim = (mx.get("debt_limits") or {}).get(stage) or {}
    cap = lim.get("max_open")
    open_items = [d for d in debt if d["status"] in (None, "open", "in_progress")]
    if cap is None:
        return _f("debt-ceiling", VERIFIED,
                  f"{len(open_items)} open debt item(s); {stage} sets no ceiling "
                  f"- debt is expected at this stage")
    if len(open_items) > cap:
        return _f("debt-ceiling", REFUTED,
                  f"{len(open_items)} open debt items against a ceiling of {cap} "
                  f"at {stage}",
                  [f"{d['id']} [{d['severity'] or 'no severity'}] {d['path']}"
                   for d in open_items[:10]],
                  [lim.get("rule", "")])
    return _f("debt-ceiling", VERIFIED,
              f"{len(open_items)} open debt item(s), within the {stage} ceiling "
              f"of {cap}")


def c_debt_severity(ctx):
    """sec.5.4 tightens severity as well as count: S4 admits no S3-or-worse, S5 no S1/S2."""
    mx, debt, stage = ctx["matrix"], ctx["debt"], ctx["stage"]
    if debt is None:
        return _f("debt-severity", SKIPPED, ctx["debt_why"])
    rule = ((mx.get("debt_limits") or {}).get(stage) or {}).get("rule", "")
    open_items = [d for d in debt if d["status"] in (None, "open", "in_progress")]
    bad = []
    if stage == "S4":
        bad = [d for d in open_items
               if d["severity"] in ("S1", "S2", "S3")]
        why = "S4 admits zero open debt at severity S3 or worse"
    elif stage == "S5":
        bad = [d for d in open_items if d["severity"] in ("S1", "S2")]
        why = "S5 admits zero open debt at severity S1 or S2"
    else:
        return _f("debt-severity", VERIFIED,
                  f"{stage} sets no severity ceiling on open debt")
    if bad:
        return _f("debt-severity", REFUTED,
                  f"{len(bad)} open debt item(s) exceed what {stage} permits - {why}",
                  [f"{d['id']} [{d['severity']}] {d['path']}" for d in bad[:10]],
                  [rule])
    return _f("debt-severity", VERIFIED,
              f"{why}; {len(open_items)} open item(s), none of them do")


def c_debt_overdue(ctx):
    """sec.5.5, No Silent Disappearance: an item past its revisit_date is a violation."""
    debt, today = ctx["debt"], ctx["today"]
    if debt is None:
        return _f("debt-overdue", SKIPPED, ctx["debt_why"])
    open_items = [d for d in debt if d["status"] in (None, "open", "in_progress")]
    over, undated = [], []
    for d in open_items:
        if not d["revisit_date"]:
            undated.append(f"{d['id']}: no revisit_date, so it can never come due "
                           f"- {d['path']}")
            continue
        try:
            when = datetime.strptime(d["revisit_date"][:10], "%Y-%m-%d").date()
        except ValueError:
            undated.append(f"{d['id']}: revisit_date {d['revisit_date']!r} is not "
                           f"a date")
            continue
        if when < today:
            over.append(f"{d['id']}: due {when.isoformat()}, "
                        f"{(today - when).days} day(s) ago, still "
                        f"{d['status'] or 'open'} - {d['path']}")
    problems = over + undated
    if problems:
        return _f("debt-overdue", REFUTED,
                  f"{len(problems)} open debt item(s) are past due or cannot come "
                  f"due: {len(over)} overdue, {len(undated)} with no usable date",
                  problems[:10],
                  ["sec.5.5: convert to a task, re-negotiate the date with "
                   "justification, or escalate to the PIC for acceptance"])
    return _f("debt-overdue", VERIFIED,
              f"all {len(open_items)} open debt item(s) carry a revisit_date that "
              f"has not passed")


def c_debt_acceptance(ctx):
    """sec.5.3: acceptance is bounded by severity and stage. S1/S2 may never be accepted."""
    debt, stage = ctx["debt"], ctx["stage"]
    if debt is None:
        return _f("debt-acceptance", SKIPPED, ctx["debt_why"])
    accepted = [d for d in debt if d["status"] == "accepted"]
    if not accepted:
        return _f("debt-acceptance", VERIFIED, "no debt has been accepted")
    here = _stage_rank(stage)
    bad = []
    for d in accepted:
        sev = d["severity"]
        if sev in ("S1", "S2"):
            bad.append(f"{d['id']} [{sev}]: S1/S2 debt may never be accepted - "
                       f"it must be resolved ({d['path']})")
            continue
        need = ACCEPT_RULES.get(sev or "")
        if need is None:
            bad.append(f"{d['id']}: severity {sev!r} is not one of S1-S4, so the "
                       f"acceptance rule cannot be applied ({d['path']})")
        elif here is not None and here < _stage_rank(need):
            bad.append(f"{d['id']} [{sev}]: acceptance of {sev} debt is permitted "
                       f"from {need} onward; this project is at {stage} "
                       f"({d['path']})")
    if bad:
        return _f("debt-acceptance", REFUTED,
                  f"{len(bad)} of {len(accepted)} accepted debt item(s) were not "
                  f"eligible for acceptance",
                  bad[:10],
                  ["S4 severity may be accepted from S3; S3 severity from S4 "
                   "with PIC sign-off; S1/S2 never"])
    return _f("debt-acceptance", VERIFIED,
              f"all {len(accepted)} accepted debt item(s) were eligible at the "
              f"stage they were accepted - eligibility only; the PIC sign-off "
              f"itself is not something a file can establish")


CHECKS = [c_scorecard_covers_stage, c_mandatory_verdicts, c_deferrals_valid,
          c_evidence_is_reference, c_debt_ceiling, c_debt_severity,
          c_debt_overdue, c_debt_acceptance]


def run(root: Path, state, today=None):
    mx = load_matrix()
    if not mx:
        return [_f("rsmr-matrix", SKIPPED,
                   f"{MATRIX_FILE} absent - run tools/extract_rsmr.py")]
    cur = (state or {}).get("current") or {}
    stage = cur.get("stage")
    if stage not in STAGES:
        return [_f("rsmr-matrix", SKIPPED,
                   f"stage {stage!r} is not one of {', '.join(STAGES)} - the "
                   f"matrix cannot be applied")]
    reg = cur.get("registers") or {}
    sc, sc_why = load_scorecard(root, reg)
    debt, debt_why = load_debt(root, reg)
    ctx = {"matrix": mx, "scorecard": sc, "scorecard_why": sc_why,
           "debt": debt, "debt_why": debt_why, "stage": stage, "root": root,
           "today": today or date.today()}
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(ctx))
        except Exception as exc:                       # noqa: BLE001
            out.append(_f(fn.__name__, SKIPPED,
                          f"check raised {type(exc).__name__}: {exc} - treat as "
                          f"unchecked, not as clean"))
    return out


def summarise(findings):
    return {
        "total": len(findings),
        "machine_checked": sum(1 for f in findings if f["status"] == VERIFIED),
        "machine_refuted": sum(1 for f in findings if f["status"] == REFUTED),
        "unverifiable": sum(1 for f in findings if f["status"] == SKIPPED),
    }


def obligations(stage):
    """(mandatory, deferrable, not_applicable) criterion lists for a stage."""
    mx = load_matrix()
    if not mx or stage not in STAGES:
        return None
    m = [c for c in mx["criteria"] if c["stages"][stage] == "M"]
    d = [c for c in mx["criteria"] if c["stages"][stage] == "D"]
    n = [c for c in mx["criteria"] if c["stages"][stage] == "NA"]
    return m, d, n


def render_scorecard(stage, project=None):
    """A scorecard whose rows are exactly the criteria the gate will judge.

    SECTION5 sec.6.2 supplies a template of 22 rows, four of them placeholders,
    against a sec.7.1 matrix of 40. sec.7.2 step 3 nonetheless instructs the
    reviewer to check "every Mandatory criterion against the scorecard". Several
    Mandatory criteria - fault-injection testing, HardFault handler, CHANGELOG
    updated per change, thermal budget measured - have no row in it to check
    against, and two more collapse a pair of matrix rows into one line
    ("Watchdog coverage verified" for both watchdog implemented and watchdog
    tested), which cannot express a PASS on one and a FAIL on the other.

    Generating from the matrix removes the mismatch rather than mapping across
    it. The sec.6.2 header fields are kept, so the artifact is still the one
    sec.6.3 describes.
    """
    obl = obligations(stage)
    if obl is None:
        return None
    mand, defr, na = obl
    mx = load_matrix()
    L = [f"# RSMR Self-Assessment Scorecard - {stage}",
         "",
         "Rows are generated from the SECTION5 sec.7.1 matrix, so every criterion",
         "the gate judges at this stage has exactly one line to answer it.",
         "Regenerate on stage change: `stage_kernel.py rsmr-scorecard`.",
         "",
         f"- **Artifact:** {project or '<task ID, module, or subsystem>'}",
         f"- **Target Stage:** {stage}",
         "- **Platform(s):** ESP32",
         "- **Assessor:** <name>",
         "- **Date:** <YYYY-MM-DD>",
         "",
         "Verdict is `PASS`, `FAIL`, or `N-A`. Evidence must be a specific",
         "reference, not a narration (sec.6.3 rule 4): `tests/reports/t-042.log:45-78`,",
         "a commit, or a record id. \"Test passed\" is not evidence.",
         "",
         "At Stage 3 and above the assessor cannot be the sole implementer",
         "(sec.6.3 rule 1) - that one is not machine-checkable and is on you.",
         ""]

    L += [f"## Mandatory at {stage} ({len(mand)})", "",
          "A FAIL here blocks the gate, and sec.7.3 forbids deferring any of them.",
          "", "| ID | Criterion | Verdict | Evidence | Debt |",
          "|---|---|---|---|---|"]
    for c in mand:
        L.append(f"| {c['id']} | {c['area']} |  |  |  |")
    if not mand:
        L.append("| | *(none mandatory at this stage)* | | | |")

    L += ["", f"## Deferrable at {stage} ({len(defr)})", "",
          "Not met is permitted, but only against a `DEBT-xxx` whose",
          "`revisit_stage` is at or before the stage in brackets.",
          "", "| ID | Criterion | Verdict | Evidence | Debt |",
          "|---|---|---|---|---|"]
    for c in defr:
        L.append(f"| {c['id']} | {c['area']} *(mandatory at "
                 f"{c['becomes_mandatory']})* |  |  |  |")
    if not defr:
        L.append("| | *(none deferrable at this stage)* | | | |")

    L += ["", f"## Not applicable at {stage} ({len(na)})", "",
          "No assessment required. Listed so that what is *not* being asked is",
          "as visible as what is - each becomes due at the stage in brackets.",
          ""]
    for c in na:
        bm = c["becomes_mandatory"] or "never"
        L.append(f"- `{c['id']}` {c['area']} *(mandatory at {bm})*")

    L += ["", "---", "",
          f"Matrix source: `{mx.get('source')}` sec.7.1, sha256 "
          f"`{str(mx.get('source_sha256'))[:12]}`.",
          "",
          "This scorecard records the engineer's assessment. The checks in",
          "`stage_kernel.py rsmr` establish that it is complete and that its",
          "deferrals are valid. Neither establishes that a criterion is met."]
    return "\n".join(L) + "\n"
