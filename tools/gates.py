#!/usr/bin/env python3
"""ESP32 Stage Kernel - gate criterion validator (phase 4).

Fills the `machine_checked` / `unverifiable` fields the digest has reported as
null since phase 1.

Two rules govern what may be claimed here, and they are the whole design:

  1. A criterion is MACHINE_CHECKED only when the check ESTABLISHES it - not
     when it establishes something adjacent. "design/icd/ contains files" does
     not establish "an ICD exists for every protocol link with typed field
     definitions and valid-range constraints". Weak signals are attached as
     HINTS to an UNVERIFIABLE criterion, where they inform a human decision
     instead of impersonating one.

  2. REFUTED is a first-class verdict and the most valuable one. "Compiles with
     zero warnings" against a build log showing two warnings is settled, against
     the project, by evidence. A gate dossier that can only say READY is an
     advocate; one that can say REFUTED is a check.

Checks anchor to criterion TEXT, not to position. If SECTION1 sec.3 is reworded
such that an anchor no longer matches, the criterion degrades to UNVERIFIABLE and
the validator reports the lost anchor. A check silently applying to the wrong
criterion would be worse than no check.
"""

from __future__ import annotations

import re
from pathlib import Path

VERIFIED = "MACHINE_CHECKED"
REFUTED = "MACHINE_REFUTED"
ATTESTED = "HUMAN_ATTESTED"
UNVERIFIABLE = "UNVERIFIABLE"


def _r(status, why, evidence=None, hints=None):
    return {"status": status, "why": why,
            "evidence": evidence or [], "hints": hints or []}


# ============================================================ checks

def c_platform_conventions(ctx):
    """Gate 1->2: correct SDK and RTOS present.

    Establishable: Arduino constructs and drivers removed in v6.0 are absent,
    and FreeRTOS task creation is actually present. Both are textual facts about
    the source tree.
    """
    import guards
    root = ctx["root"]
    srcs = [p for p in root.rglob("*.c")] + [p for p in root.rglob("*.h")] \
        + [p for p in root.rglob("*.cpp")]
    srcs = [p for p in srcs if guards._is_source(str(p))]
    if not srcs:
        return _r(UNVERIFIABLE, "no C/C++ sources found under the project")
    # The cap bounds a pathological tree. It also means a VERIFIED verdict below
    # would be claiming "no violations in N files" having read only 400 of them -
    # a check overstating its own coverage, which is the one thing a gate verdict
    # must never do. So the count reported is the count scanned, and truncation
    # is disqualifying rather than cosmetic.
    SCAN_CAP = 400
    scanned = srcs[:SCAN_CAP]
    truncated = len(srcs) - len(scanned)
    bad, rtos_firmware, rtos_testonly = [], [], []
    for p in scanned:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if re.search(r"\bxTaskCreate", text):
            rel = p.relative_to(root).as_posix()
            (rtos_firmware if guards.is_firmware_source(rel)
             else rtos_testonly).append(rel)
        for f in guards.g_arduino(str(p), text, ctx) + \
                guards.g_legacy_driver(str(p), text, ctx):
            bad.append(f"{p.relative_to(root).as_posix()}:{f.get('line')} "
                       f"[{f['guard']}] {f['message'][:90]}")
    if bad:
        return _r(REFUTED, "platform convention violations found in source",
                  evidence=bad[:8])
    if not rtos_firmware:
        if rtos_testonly:
            return _r(UNVERIFIABLE,
                      f"xTaskCreate* appears only in test or host-abstraction "
                      f"code, which does not establish what runs on the device",
                      evidence=rtos_testonly[:6],
                      hints=["move the check to firmware, or attest this "
                             "criterion - a host-side stub creating a task is "
                             "not the firmware creating a task"])
        return _r(UNVERIFIABLE,
                  "no xTaskCreate* call found - FreeRTOS use is not established "
                  "by the source alone",
                  hints=[f"{len(scanned)} of {len(srcs)} source files scanned, "
                         f"none create a task"])
    if truncated:
        return _r(UNVERIFIABLE,
                  f"{len(scanned)} of {len(srcs)} source files scanned - the "
                  f"remaining {truncated} were not read, so the absence of "
                  f"violations across the tree is not established",
                  hints=[f"raise SCAN_CAP in gates.py, or split the project into "
                         f"components so each is scanned within the cap"])
    return _r(VERIFIED,
              f"all {len(srcs)} source files: no Arduino constructs, no drivers "
              f"removed in ESP-IDF v6.0, FreeRTOS task creation present in "
              f"firmware",
              evidence=[f"task created in {p}" for p in rtos_firmware[:3]],
              hints=([f"{len(rtos_testonly)} further call(s) sit in test or host "
                      f"code and were not counted toward this"]
                     if rtos_testonly else []))


def c_assumptions_owned(ctx):
    """Gate 1->2: every unresolved ambiguity logged with owner + deadline.

    Two things this got wrong. It checked the deadline and not the owner, while
    SECTION1 asks for both - at sec.3, at sec.4, and again in the Stage 2+
    checklist. And an empty log returned MACHINE_CHECKED "no open assumptions",
    which reads the absence of records as the absence of ambiguities. A project
    that logged nothing is indistinguishable from one that had nothing to log,
    and only one of those satisfies the criterion.
    """
    state = ctx.get("state")
    if not state:
        return _r(UNVERIFIABLE, "no stage-state.yaml to read")
    events = [e for e in (state.get("log") or []) if isinstance(e, dict)]
    opened = {e.get("id"): e for e in events
              if e.get("event") == "assumption_opened"}
    resolved = {e.get("id") for e in events
                if e.get("event") == "assumption_resolved"}
    open_ids = [i for i in opened if i not in resolved]

    if not opened:
        return _r(UNVERIFIABLE,
                  "the log records no assumption ever opened, so nothing "
                  "distinguishes a project with no unresolved ambiguity from "
                  "one that logged none",
                  hints=["this criterion is about the original business ask; "
                         "if it genuinely held no ambiguity, attest that rather "
                         "than leaving it to an empty log"])
    if not open_ids:
        return _r(VERIFIED,
                  f"all {len(opened)} assumption(s) ever opened are resolved - "
                  f"none is outstanding",
                  evidence=[f"{i} resolved" for i in list(opened)[:6]])

    no_owner = [i for i in open_ids if not opened[i].get("owner")]
    no_deadline = [i for i in open_ids if not opened[i].get("deadline")]
    if no_owner or no_deadline:
        ev = [f"{i}: no owner" for i in no_owner[:5]]
        ev += [f"{i}: no deadline" for i in no_deadline[:5]]
        return _r(REFUTED,
                  f"of {len(open_ids)} open assumption(s), {len(no_owner)} have "
                  f"no owner and {len(no_deadline)} no deadline",
                  evidence=ev,
                  hints=["SECTION1 sec.3 asks for both; an assumption with a "
                         "deadline and no owner has a date nobody is answerable "
                         "for"])
    return _r(VERIFIED,
              f"all {len(open_ids)} open assumption(s) carry an owner and a "
              f"deadline",
              hints=[f"{i} -> {opened[i].get('owner')}, due "
                     f"{opened[i].get('deadline')}" for i in open_ids[:6]])


def c_zero_warnings(ctx):
    """Gate 2->3: compiles with zero warnings.

    The strongest check in the set: an archived build log settles it either way.
    Absent a log the answer is unknown - never zero.
    """
    targets = [t for t in ctx.get("targets", []) if t.get("configured")]
    if not targets:
        return _r(UNVERIFIABLE, "no configured target to build")
    unlogged, unbound, warned, clean = [], [], [], []
    for t in targets:
        lb = t.get("last_build") or {}
        w = lb.get("warnings")
        binding = lb.get("log_binding")
        if w is None:
            # A log that exists but does not describe the current tree is not
            # the same as no log at all, and the engineer needs to know which.
            if lb.get("log") and binding:
                unbound.append(f"{t['target']}: {lb['log']} - {binding}")
            else:
                unlogged.append(t["target"])
        elif w > 0:
            warned.append(f"{t['target']}: {w} warning(s) in {lb.get('log')}")
        else:
            clean.append(f"{t['target']}: 0 warnings in {lb.get('log')}"
                         + (f" ({binding})" if binding else ""))
    if warned:
        return _r(REFUTED, "compiler warnings present in an archived build log",
                  evidence=warned)
    if unbound:
        return _r(UNVERIFIABLE,
                  f"a build log exists but does not establish this criterion "
                  f"for: {', '.join(u.split(':')[0] for u in unbound)}",
                  evidence=unbound + clean,
                  hints=["rebuild to bind a log to the current source tree - a "
                         "clean log over stale or uncompiled sources is not "
                         "evidence of a clean build"])
    if unlogged:
        return _r(UNVERIFIABLE,
                  f"no archived build log for: {', '.join(unlogged)}",
                  evidence=clean,
                  hints=["run tools/idf_run.ps1 -Target <t> build to archive one"])
    return _r(VERIFIED, "zero warnings across every configured target",
              evidence=clean)


# SECTION2 sec.6.5 fixes the ICD format, field by field. Two of the fields are
# conditional on the QoS class, which is what makes this checkable rather than
# a keyword hunt.
RE_ICD_HEAD = re.compile(r"^\s*ICD-([A-Za-z0-9_-]+)\s*:", re.M)
ICD_FIELDS = {
    "qos": r"QoS class\s*:\s*(.+)",
    "retry_count": r"Retry count\s*:\s*(.+)",
    "retry_interval": r"Retry interval\s*:\s*(.+)",
    "schema_version": r"Schema version\s*:\s*(.+)",
    "link_to": r"Link to\s*:\s*(.+)",
}
QOS_CLASSES = ("best-effort", "at-least-once", "exactly-once")
RE_TBD = re.compile(r"\bTBD\b", re.I)
RE_ASM = re.compile(r"\bASM-S\d-\d{3}\b")


def c_no_tbd(ctx):
    """Gate 2->3: retry/QoS/backoff stated for every link - no TBD remains.

    This read the documents for the literal string TBD and reported
    MACHINE_CHECKED when it found none. A connectivity document mentioning
    retry, QoS and backoff exactly zero times passed that way: absence of the
    word TBD is not presence of the parameters, and the criterion names both.

    It was also stricter than the specification in the other direction. sec.6.5
    permits a TBD field at the current stage provided it is logged as an
    ASM-<STAGE>-<NNN>, and a bare TBD scan refuses that.

    sec.6.5 fixes the ICD format precisely enough to check field by field,
    including the two fields whose necessity depends on the QoS class.
    """
    root = ctx["root"]
    dirs = [root / "design" / "connectivity", root / "design" / "icd"]
    files = [p for d in dirs if d.is_dir() for p in d.rglob("*.md")]
    if not files:
        return _r(UNVERIFIABLE,
                  "design/connectivity/ and design/icd/ hold no documents")

    links, docs_without_icd, missing, bad_qos, bare_tbd = [], [], [], [], []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = p.relative_to(root).as_posix()
        heads = list(RE_ICD_HEAD.finditer(text))
        if not heads:
            docs_without_icd.append(rel)
            continue
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            block, link = text[h.start():end], h.group(1)
            links.append(f"{rel}:ICD-{link}")
            vals = {}
            for key, pat in ICD_FIELDS.items():
                m = re.search(pat, block, re.I)
                vals[key] = m.group(1).strip() if m else None
                if not vals[key]:
                    missing.append(f"{rel} ICD-{link}: no "
                                   f"'{pat.split(chr(92))[0].strip()}' field")
            q = (vals.get("qos") or "").lower()
            cls = next((c for c in QOS_CLASSES if c in q), None)
            if vals.get("qos") and not cls and not RE_TBD.search(q):
                bad_qos.append(f"{rel} ICD-{link}: QoS class {vals['qos']!r} is "
                               f"not one of {', '.join(QOS_CLASSES)}")
            # Conditional fields, per sec.6.5's own parenthetical notes.
            if cls in ("at-least-once", "exactly-once") and \
                    not re.search(r"Ack timeout\s*:\s*\S", block, re.I):
                missing.append(f"{rel} ICD-{link}: QoS is {cls}, which sec.6.5 "
                               f"requires an 'Ack timeout' for")
            if cls == "exactly-once" and \
                    not re.search(r"Dedup window\s*:\s*\S", block, re.I):
                missing.append(f"{rel} ICD-{link}: QoS is exactly-once, which "
                               f"sec.6.5 requires a 'Dedup window' for")
            # sec.6.5 permits a TBD field, but only against a logged assumption.
            if RE_TBD.search(block) and not RE_ASM.search(block):
                ln = text.count(chr(10), 0, h.start()) + 1
                bare_tbd.append(f"{rel}:{ln} ICD-{link}: TBD with no "
                                f"ASM-S<n>-<NNN> - sec.6.5 permits the TBD only "
                                f"when it is logged as an assumption")

    if not links:
        return _r(UNVERIFIABLE,
                  f"{len(files)} connectivity/ICD document(s), none containing an "
                  f"'ICD-<LINKID>:' block - no link is described in the sec.6.5 "
                  f"format, so nothing here states retry, QoS, or backoff",
                  evidence=docs_without_icd[:6],
                  hints=["SECTION2 sec.6.5 gives the block format"])
    problems = bare_tbd + missing + bad_qos
    if problems:
        return _r(REFUTED,
                  f"{len(problems)} problem(s) across {len(links)} declared link(s): "
                  f"{len(bare_tbd)} unlogged TBD, {len(missing)} missing field, "
                  f"{len(bad_qos)} QoS class outside the three sec.6.5 allows",
                  evidence=problems[:8],
                  hints=([f"{len(docs_without_icd)} document(s) carry no ICD block "
                          f"at all: {', '.join(docs_without_icd[:3])}"]
                         if docs_without_icd else []))
    return _r(VERIFIED,
              f"all {len(links)} declared link(s) state QoS class, retry count, "
              f"retry interval with backoff, schema version and a requirement "
              f"link; conditional Ack timeout and Dedup window present where the "
              f"QoS class requires them",
              evidence=links[:6],
              hints=(["fields are present and well-formed; whether the values are "
                      "right for the link is not something the document can show"]
                     + ([f"{len(docs_without_icd)} document(s) in these folders "
                         f"carry no ICD block and were not assessed: "
                         f"{', '.join(docs_without_icd[:3])}"]
                        if docs_without_icd else [])))


# ============================================================ registry

# anchor: a regex that must match the criterion text in SECTION1 sec.3.
CHECKS = [
    {"id": "platform-conventions", "gate": "1->2",
     "anchor": r"Platform conventions.*verified", "fn": c_platform_conventions},
    {"id": "assumptions-owned", "gate": "1->2",
     "anchor": r"unresolved ambiguities.*logged with owner", "fn": c_assumptions_owned},
    {"id": "zero-warnings", "gate": "2->3",
     "anchor": r"compiles with .*-Werror.*zero warnings", "fn": c_zero_warnings},
    {"id": "no-tbd", "gate": "2->3",
     "anchor": r"Retry/QoS/backoff.*no .?TBD.? remain", "fn": c_no_tbd},
]


def parse_criteria(spec_dir: Path, gate: str):
    """Read the criterion lines for one gate from SECTION1 sec.3."""
    p = spec_dir / "WORKFLOW_SECTION1.md"
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    a, b = gate.split("->")
    m = re.search(rf"^###\s+Gate\s+{a}\s*(?:→|->)\s*{b}\b", text, re.M)
    if not m:
        return None
    nxt = re.search(r"^###\s+", text[m.end():], re.M)
    block = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]
    return [ln.strip()[4:].strip()
            for ln in block.splitlines() if ln.strip().startswith("[ ] ")]


def evaluate(gate: str, criteria, ctx, attestations):
    """Classify every criterion of one gate. Never returns PASS."""
    att_by_gate = {}
    for a in (attestations or []):
        if isinstance(a, dict) and a.get("gate") == gate:
            att_by_gate.setdefault(a.get("criterion") or "", []).append(a)

    used, rows = set(), []
    for text in criteria:
        row = {"criterion": text, "status": UNVERIFIABLE,
               "why": "no machine check establishes this criterion",
               "evidence": [], "hints": []}
        for chk in CHECKS:
            if chk["gate"] != gate:
                continue
            if re.search(chk["anchor"], text, re.I):
                used.add(chk["id"])
                try:
                    res = chk["fn"](ctx)
                except Exception as exc:                # noqa: BLE001
                    res = _r(UNVERIFIABLE,
                             f"check {chk['id']} raised "
                             f"{type(exc).__name__}: {exc} - treat as unchecked")
                row.update(res)
                row["check"] = chk["id"]
                break
        if row["status"] == UNVERIFIABLE:
            for crit_id, atts in att_by_gate.items():
                if crit_id and crit_id.lower() in text.lower():
                    row["status"] = ATTESTED
                    row["why"] = f"attested by {atts[0].get('attested_by')} " \
                                 f"on {atts[0].get('date')} ({atts[0].get('method')})"
                    break
        rows.append(row)

    orphans = [c["id"] for c in CHECKS
               if c["gate"] == gate and c["id"] not in used]
    return rows, orphans


def summarise(rows):
    return {
        "total": len(rows),
        "machine_checked": sum(1 for r in rows if r["status"] == VERIFIED),
        "machine_refuted": sum(1 for r in rows if r["status"] == REFUTED),
        "human_attested": sum(1 for r in rows if r["status"] == ATTESTED),
        "unverifiable": sum(1 for r in rows if r["status"] == UNVERIFIABLE),
    }


def recommendation(summary):
    """READY / NOT-READY only. PASS is a human utterance (invariant I3)."""
    if summary["machine_refuted"]:
        return "NOT-READY", (f"{summary['machine_refuted']} criterion(s) refuted "
                             f"by evidence")
    if summary["unverifiable"]:
        return "NOT-READY", (f"{summary['unverifiable']} criterion(s) neither "
                             f"machine-checked nor attested")
    return "READY", "every criterion is machine-checked or attested"


# ============================================================ design review (sec.8)
#
# SECTION2 sec.8 is NOT a SECTION1 gate. It runs INSIDE a stage, and its FAIL
# edge returns to the Measurable Requirements Table rather than to the artifact
# that failed. The machinery transfers unchanged - four verdicts, anchor-to-text,
# the adversary, the dossier - but the vocabulary and the log event differ.
#
# Its most valuable output is the UNVERIFIABLE count, and that needs no checks at
# all: of 44 items, 17 make universal claims ("every", "all", "each") over sets
# no file enumerates. Reporting that honestly is the product.

DESIGN_REVIEW = "design-review"


def _dc(root, state, which):
    """Reuse a design_check finding as a sec.8 verdict."""
    import design_check
    reg = ((state or {}).get("current") or {}).get("registers") or {}
    for fn in design_check.CHECKS:
        if fn.__name__ == which:
            r = fn(root, state, reg)
            return _r({design_check.VERIFIED: VERIFIED,
                       design_check.REFUTED: REFUTED,
                       design_check.SKIPPED: UNVERIFIABLE}[r["status"]],
                      r["why"], evidence=r["evidence"])
    return _r(UNVERIFIABLE, f"design check {which} not found")


def d_req_table(ctx):
    return _dc(ctx["root"], ctx.get("state"), "c_req_table_shape")


def d_decisions(ctx):
    return _dc(ctx["root"], ctx.get("state"), "c_decision_records")


def d_orphans(ctx):
    return _dc(ctx["root"], ctx.get("state"), "c_orphan_requirements")


def d_assumptions_owned(ctx):
    return c_assumptions_owned(ctx)


def d_claudemd_section(ctx):
    """sec.8 cites CLAUDE.md sec.2 / sec.5. Check the file actually has them.

    This is REFUTED today and is the cheapest demonstration that a review can
    settle a criterion against the project rather than merely record an opinion.
    """
    root = ctx["root"]
    cands = [root / "CLAUDE.md"]
    spec = ctx.get("spec_dir")
    if spec:
        cands += [Path(spec).parent / "CLAUDE.md", Path(spec) / "CLAUDE.md"]
    for f in cands:
        if Path(f).is_file():
            text = Path(f).read_text(encoding="utf-8", errors="ignore")
            heads = re.findall(r"^#{1,3}\s*\d+\.|^\s*§\s*\d+", text, re.M)
            if heads:
                return _r(VERIFIED,
                          f"{Path(f).name} carries {len(heads)} numbered section(s)")
            return _r(REFUTED,
                      f"{Path(f).name} has no numbered sections, so this "
                      f"criterion cites an authority that does not exist",
                      evidence=[f"{Path(f).name}: headings are "
                                f"[SYSTEM]/[CONSTRAINTS] markers, not numbered"])
    return _r(UNVERIFIABLE, "CLAUDE.md not reachable from here")


DESIGN_CHECKS = [
    {"id": "req-table-format", "gate": DESIGN_REVIEW,
     "anchor": r"requirements in measurable-table format", "fn": d_req_table},
    {"id": "assumptions-owned-dr", "gate": DESIGN_REVIEW,
     "anchor": r"assumption has owner \+ deadline", "fn": d_assumptions_owned},
    {"id": "req-drives-artifact", "gate": DESIGN_REVIEW,
     "anchor": r"requirement drives at least one downstream artifact",
     "fn": d_orphans},
    {"id": "decision-format", "gate": DESIGN_REVIEW,
     "anchor": r"platform decision documented in .*3\.1 format",
     "fn": d_decisions},
    {"id": "claudemd-conventions", "gate": DESIGN_REVIEW,
     "anchor": r"Platform conventions from CLAUDE\.md", "fn": d_claudemd_section},
    {"id": "claudemd-connectivity", "gate": DESIGN_REVIEW,
     "anchor": r"Connectivity template from CLAUDE\.md", "fn": d_claudemd_section},
]


def parse_design_review(spec_dir: Path):
    """Checklist items from SECTION2 sec.8. Anchored to text, like the gates."""
    f = Path(spec_dir) / "WORKFLOW_SECTION2.md"
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(r"^##\s+8\.", text, re.M)
    if not m:
        return None
    block_text = text[m.end():]
    items = []
    for ln in block_text.splitlines():
        st = ln.strip()
        # "Target Stage: [ ] S1  [ ] S2 ..." is a stage SELECTOR, not a
        # criterion. Several boxes on one line is what distinguishes it.
        if st.count("[ ]") > 1:
            continue
        if st.startswith("[ ] "):
            items.append(re.sub(r"^\[ \]\s*", "", st))
        elif items and st and not st.startswith(("[", "#", "`"))                 and not re.match(r"^[A-Z][\w /]+\(.\d\)\s*:$", st):
            # a wrapped continuation of the criterion above
            items[-1] = items[-1].rstrip() + " " + st
    return items or None


def evaluate_design_review(criteria, ctx, attestations):
    """Same four-verdict shape as a gate, against the sec.8 checklist."""
    saved = CHECKS[:]
    try:
        CHECKS[:] = DESIGN_CHECKS
        rows, orphans = evaluate(DESIGN_REVIEW, criteria, ctx, attestations)
    finally:
        CHECKS[:] = saved
    universal = sum(1 for r in rows
                    if re.search(r"\b(every|all|each)\b", r["criterion"], re.I))
    return rows, orphans, universal
