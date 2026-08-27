#!/usr/bin/env python3
"""ESP32 Stage Kernel - design artifact shape checks (Section 2, context layer).

These checks establish SHAPE, and nothing more. They can tell you that a
requirement ID is malformed, that a decision record is missing a mandatory
field, or that an artifact cites a requirement that does not exist. They cannot
tell you whether a requirement is the right requirement, or whether a measured
target is true. That boundary is deliberate and is stated in every finding.

Everything here is blocked on one thing: knowing WHERE the artifacts are.
SECTION 7 sec.4.1 already fixes the layout; SECTION 2 simply never references
it. `current.registers` in stage-state.yaml is the bridge, which is why the
context layer had to come first.

Formats are taken verbatim from the specification:
  SECTION2 sec.2.1 - requirements table, seven columns
  SECTION2 sec.3.1 - decision record, five fields, nine bracket categories
"""

from __future__ import annotations

import re
from pathlib import Path

# --- SECTION2 sec.2.1 --------------------------------------------------------
REQ_COLUMNS = ["id", "requirement", "measurable target", "drives", "source",
               "assumption", "stage gate"]
# Optional, and checked only when present: SECTION2 sec.2.1 does not
# mandate it. Adding it is a local decision, and the checks say so.
REQ_COLUMN_ATTRIBUTE = "attribute"
RE_REQ_ID = re.compile(r"^REQ-S\d-\d{3}$")
RE_ASM_ID = re.compile(r"^ASM-S\d-\d{3}")
RE_DRIVES = re.compile(r"\b(HW|FW|TEST|SCH)-\d+\b")
RE_SOURCE = re.compile(r"\b(BA|INC|AD)-[\w-]+\b|\bREQ-S\d-\d{3}\b")
RE_GATE = re.compile(r"Gate\s*\d\s*(?:->|→)\s*\d", re.I)

# --- SECTION2 sec.3.1 --------------------------------------------------------
AD_FIELDS = ["Decision:", "Driven by:", "Technical reason:",
             "Alternatives considered:", "Stage impact:"]
AD_CATEGORIES = {
    "peripheral capability", "power budget", "timing constraint",
    "memory constraint", "connectivity requirement", "ecosystem constraint",
    "regulatory requirement", "supply-chain constraint", "cost constraint",
}
RE_AD_FILE = re.compile(r"^AD-S\d-\d{3}-[a-z0-9-]+\.md$")

VERIFIED = "MACHINE_CHECKED"
REFUTED = "MACHINE_REFUTED"
SKIPPED = "UNVERIFIABLE"


def _f(check, status, why, evidence=None, hints=None):
    return {"check": check, "status": status, "why": why,
            "evidence": evidence or [], "hints": hints or []}


# ============================================================ table parsing

def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_sep(line):
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def parse_tables(text):
    """Yield (header_lower, rows, first_row_lineno) for every markdown table."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        if lines[i].strip().startswith("|") and i + 1 < len(lines) \
                and _is_sep(lines[i + 1]):
            header = [h.lower() for h in _split_row(lines[i])]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append((j + 1, _split_row(lines[j])))
                j += 1
            out.append((header, rows, i + 1))
            i = j
        else:
            i += 1
    return out


def _requirements_table(text):
    """The requirements table is the one whose header carries ID + Measurable
    Target. A document may legitimately contain other tables."""
    for header, rows, ln in parse_tables(text):
        if "id" in header and any("measurable target" in h for h in header):
            return header, rows, ln
    return None, None, None


# ============================================================ checks

def c_registers_present(root, state, reg):
    """Every path named in current.registers exists on disk."""
    if not reg:
        return _f("registers-present", SKIPPED,
                  "current.registers names no design artifact paths - "
                  "SECTION7 sec.4.1 fixes the layout, stage-state.yaml must "
                  "point at it")
    missing = [f"{k}: {v}" for k, v in reg.items()
               if not (root / str(v)).exists()]
    if missing:
        return _f("registers-present", REFUTED,
                  f"{len(missing)} of {len(reg)} register paths do not exist",
                  missing)
    return _f("registers-present", VERIFIED,
              f"all {len(reg)} register paths exist")


def c_req_table_shape(root, state, reg):
    """SECTION2 sec.2.1: seven columns, well-formed IDs, closed vocabularies."""
    p = reg.get("requirements")
    if not p:
        return _f("req-table-shape", SKIPPED, "registers.requirements not set")
    f = root / str(p)
    if not f.is_file():
        return _f("req-table-shape", SKIPPED, f"{p} does not exist")
    header, rows, _ = _requirements_table(f.read_text(encoding="utf-8",
                                                      errors="ignore"))
    if header is None:
        return _f("req-table-shape", REFUTED,
                  f"{p} contains no table with ID and Measurable Target columns")

    bad = []
    for want in REQ_COLUMNS:
        if not any(want in h for h in header):
            bad.append(f"missing column: {want}")
    idx = {name: n for n, h in enumerate(header)
           for name in REQ_COLUMNS if name in h}

    seen = {}
    for ln, cells in rows:
        def cell(name):
            n = idx.get(name)
            return cells[n] if n is not None and n < len(cells) else ""
        rid = cell("id")
        if not RE_REQ_ID.match(rid):
            bad.append(f"{p}:{ln} malformed ID {rid!r} (want REQ-S<n>-<NNN>)")
            continue
        if rid in seen:
            bad.append(f"{p}:{ln} duplicate ID {rid} (first at line {seen[rid]})")
        seen[rid] = ln
        if not RE_DRIVES.search(cell("drives")):
            bad.append(f"{p}:{ln} {rid} Drives has no HW-/FW-/TEST-/SCH- id")
        if not RE_SOURCE.search(cell("source")):
            bad.append(f"{p}:{ln} {rid} Source is not BA-/INC-/AD-/REQ-")
        a = cell("assumption")
        if a.upper() != "NONE" and not RE_ASM_ID.match(a):
            bad.append(f"{p}:{ln} {rid} Assumption is neither NONE nor ASM-S<n>-<NNN>")
        if not RE_GATE.search(cell("stage gate")):
            bad.append(f"{p}:{ln} {rid} Stage Gate does not name a gate")

    if bad:
        return _f("req-table-shape", REFUTED,
                  f"{len(bad)} shape violation(s) across {len(rows)} requirement(s)",
                  bad[:10])
    return _f("req-table-shape", VERIFIED,
              f"{len(rows)} requirement(s): seven columns present, IDs unique "
              f"and well-formed, vocabularies closed")


def c_decision_records(root, state, reg):
    """SECTION2 sec.3.1: five mandatory fields, nine bracket categories."""
    p = reg.get("decisions")
    if not p:
        return _f("decision-records", SKIPPED, "registers.decisions not set")
    d = root / str(p)
    if not d.is_dir():
        return _f("decision-records", SKIPPED, f"{p} is not a directory")
    files = sorted(d.glob("*.md"))
    if not files:
        return _f("decision-records", SKIPPED, f"{p} holds no decision records")

    bad = []
    for fp in files:
        rel = fp.relative_to(root).as_posix()
        if not RE_AD_FILE.match(fp.name):
            bad.append(f"{rel} filename is not AD-S<n>-<NNN>-<slug>.md")
        text = fp.read_text(encoding="utf-8", errors="ignore")
        for field in AD_FIELDS:
            if field not in text:
                bad.append(f"{rel} missing mandatory field {field!r}")
        cats = re.findall(r"Technical reason:\s*\[([^:\]]+)[:\]]", text)
        for c in cats:
            if c.strip().lower() not in AD_CATEGORIES:
                bad.append(f"{rel} category [{c.strip()}] is not one of the "
                           f"nine in SECTION2 sec.3.1")
    if bad:
        return _f("decision-records", REFUTED,
                  f"{len(bad)} violation(s) across {len(files)} record(s)",
                  bad[:10])
    return _f("decision-records", VERIFIED,
              f"{len(files)} decision record(s): filenames, five fields and "
              f"bracket categories all conform")


def _known_req_ids(root, reg):
    p = reg.get("requirements")
    if not p or not (root / str(p)).is_file():
        return None
    _, rows, _ = _requirements_table((root / str(p))
                                     .read_text(encoding="utf-8", errors="ignore"))
    if rows is None:
        return None
    return {c[0] for _, c in rows if c and RE_REQ_ID.match(c[0])}


def _design_docs(root, reg):
    out = []
    for key, val in (reg or {}).items():
        if key == "requirements":
            continue
        t = root / str(val)
        if t.is_dir():
            out += [q for q in t.rglob("*.md")]
        elif t.is_file() and t.suffix == ".md":
            out.append(t)
    return out


def c_req_references_resolve(root, state, reg):
    """Every REQ- cited by a design artifact resolves to the register.

    This is the traceability backbone SECTION1 sec.5 calls for, and the first
    check that can establish 'a broken chain is a gate failure'."""
    known = _known_req_ids(root, reg)
    if known is None:
        return _f("req-references-resolve", SKIPPED,
                  "requirements register unreadable - nothing to resolve against")
    docs = _design_docs(root, reg)
    if not docs:
        return _f("req-references-resolve", SKIPPED,
                  "no design documents found under the registered paths")
    dangling = []
    for fp in docs:
        text = fp.read_text(encoding="utf-8", errors="ignore")
        rel = fp.relative_to(root).as_posix()
        for m in re.finditer(r"\bREQ-S\d-\d{3}\b", text):
            if m.group(0) not in known:
                ln = text.count("\n", 0, m.start()) + 1
                dangling.append(f"{rel}:{ln} cites {m.group(0)}, absent from the register")
    if dangling:
        return _f("req-references-resolve", REFUTED,
                  f"{len(dangling)} citation(s) resolve to no requirement",
                  dangling[:10])
    return _f("req-references-resolve", VERIFIED,
              f"every REQ- citation across {len(docs)} document(s) resolves to "
              f"one of {len(known)} registered requirement(s)")


def c_orphan_requirements(root, state, reg):
    """A requirement no artifact references is a requirement the design ignores."""
    known = _known_req_ids(root, reg)
    if known is None:
        return _f("orphan-requirements", SKIPPED, "requirements register unreadable")
    docs = _design_docs(root, reg)
    if not docs:
        return _f("orphan-requirements", SKIPPED, "no design documents to search")
    cited = set()
    for fp in docs:
        cited |= set(re.findall(r"\bREQ-S\d-\d{3}\b",
                                fp.read_text(encoding="utf-8", errors="ignore")))
    orphans = sorted(known - cited)
    if orphans:
        return _f("orphan-requirements", REFUTED,
                  f"{len(orphans)} requirement(s) drive no design artifact",
                  orphans[:10])
    return _f("orphan-requirements", VERIFIED,
              f"all {len(known)} requirement(s) are referenced by at least one artifact")


def c_assumption_references(root, state, reg):
    """Every ASM- cited anywhere resolves to the assumption register."""
    p = reg.get("assumptions")
    if not p:
        return _f("assumption-references", SKIPPED, "registers.assumptions not set")
    f = root / str(p)
    if not f.is_file():
        return _f("assumption-references", REFUTED,
                  f"registers.assumptions points at {p}, which does not exist - "
                  f"assumptions folded from the log have no register to live in",
                  [str(p)])
    known = set(re.findall(r"\bASM-S\d-\d{3}\b",
                           f.read_text(encoding="utf-8", errors="ignore")))
    dangling = []
    for fp in _design_docs(root, reg) + [root / str(reg["requirements"])] \
            if reg.get("requirements") else _design_docs(root, reg):
        if not Path(fp).is_file():
            continue
        text = Path(fp).read_text(encoding="utf-8", errors="ignore")
        rel = Path(fp).relative_to(root).as_posix()
        for m in re.finditer(r"\bASM-S\d-\d{3}\b", text):
            if m.group(0) not in known:
                ln = text.count("\n", 0, m.start()) + 1
                dangling.append(f"{rel}:{ln} cites {m.group(0)}, absent from the register")
    if dangling:
        return _f("assumption-references", REFUTED,
                  f"{len(dangling)} assumption citation(s) resolve to nothing",
                  dangling[:10])
    return _f("assumption-references", VERIFIED,
              f"every ASM- citation resolves to one of {len(known)} registered "
              f"assumption(s)")




# ============================================================ quality attributes
#
# SECTION1's stage bar is expressed in RSMR - four of the twelve attributes in
# the cross-platform reference. SECTION5 supplies measurable criteria for those
# four and for no others. SECTION2's requirements table binds to neither, so a
# Measurable Target is a number with no stated relationship to the quality it is
# meant to buy.
#
# Adding an Attribute column closes that: the requirement names what quality it
# purchases, the vocabulary is closed so it cannot be invented, and the conflict
# graph shows what the purchase costs elsewhere.

ATTR_FILE = "quality-attributes.yaml"
RE_CRITERION = re.compile(r"\b(R|S|M|RL)-(ESP|S32|RPI)-\d{2}\b")


def load_attributes():
    f = Path(__file__).resolve().parent.parent / ATTR_FILE
    if not f.is_file():
        return None
    try:
        import yaml
        return yaml.safe_load(f.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return None


def _attr_cells(root, reg):
    """(line, [attribute names], target_cell) per requirement row."""
    pth = reg.get("requirements")
    if not pth or not (root / str(pth)).is_file():
        return None, None
    f = root / str(pth)
    header, rows, _ = _requirements_table(f.read_text(encoding="utf-8",
                                                      errors="ignore"))
    if header is None:
        return None, None
    ai = next((n for n, h in enumerate(header) if "attribute" in h), None)
    ti = next((n for n, h in enumerate(header) if "measurable target" in h), None)
    ii = next((n for n, h in enumerate(header) if h.strip() == "id"), 0)
    out = []
    for ln, cells in rows:
        def cell(n):
            return cells[n] if n is not None and n < len(cells) else ""
        names = [x.strip() for x in cell(ai).split(",") if x.strip()] if ai is not None else []
        out.append((ln, cell(ii), names, cell(ti)))
    return out, (ai is not None)


def c_attribute_vocabulary(root, state, reg):
    """Attribute names come from a closed set of twelve. Nothing invented."""
    spec = load_attributes()
    if not spec:
        return _f("attribute-vocabulary", SKIPPED,
                  f"{ATTR_FILE} absent - run tools/extract_attrs.py")
    rows, has_col = _attr_cells(root, reg)
    if rows is None:
        return _f("attribute-vocabulary", SKIPPED, "requirements table unreadable")
    if not has_col:
        return _f("attribute-vocabulary", SKIPPED,
                  "the requirements table has no Attribute column - a Measurable "
                  "Target then states a number with no stated relationship to the "
                  "quality it buys. SECTION2 sec.2.1 does not mandate this column; "
                  "adding it is a local decision")
    known = {a["name"] for a in spec["attributes"]}
    bad = [f"{rid} line {ln}: {n!r} is not one of the twelve"
           for ln, rid, names, _ in rows for n in names if n not in known]
    if bad:
        return _f("attribute-vocabulary", REFUTED,
                  f"{len(bad)} attribute name(s) outside the closed vocabulary",
                  bad[:8])
    total = sum(len(n) for _, _, n, _ in rows)
    return _f("attribute-vocabulary", VERIFIED,
              f"{total} attribute claim(s) across {len(rows)} requirement(s), all "
              f"within the twelve")


def c_attribute_measurable(root, state, reg):
    """A requirement claiming an attribute with no criteria is unverifiable.

    SECTION5 covers RSMR only. Eight of the twelve attributes have no pass
    condition anywhere, so a requirement resting on one cannot be settled by
    evidence - which is worth stating rather than discovering at a gate.
    """
    spec = load_attributes()
    if not spec:
        return _f("attribute-measurable", SKIPPED, f"{ATTR_FILE} absent")
    rows, has_col = _attr_cells(root, reg)
    if rows is None or not has_col:
        return _f("attribute-measurable", SKIPPED, "no Attribute column to read")
    crit = {a["name"]: (a.get("criteria_esp32") or []) for a in spec["attributes"]}
    unmeasurable, unnamed = [], []
    for ln, rid, names, target in rows:
        if not names:
            unnamed.append(f"{rid} line {ln}: names no attribute")
            continue
        if not any(crit.get(n) for n in names):
            unmeasurable.append(f"{rid} line {ln}: claims {', '.join(names)} - no "
                                f"measurable criteria exist for any of them")
    if unnamed:
        return _f("attribute-measurable", REFUTED,
                  f"{len(unnamed)} requirement(s) name no attribute at all",
                  unnamed[:8])
    if unmeasurable:
        return _f("attribute-measurable", REFUTED,
                  f"{len(unmeasurable)} requirement(s) rest only on attributes with "
                  f"no measurable criteria - unverifiable by construction",
                  unmeasurable[:8])
    return _f("attribute-measurable", VERIFIED,
              f"every requirement claims at least one attribute that SECTION5 can "
              f"measure")


def c_attribute_conflicts(root, state, reg):
    """Surface the trade-offs the requirement set has already bought into.

    This is the reference's unique contribution: 44 CONFLICTS edges. Detecting
    that a project demands both sides of one is not a failure - it is the thing
    that makes a project's feasibility visible while it can still be changed.
    """
    spec = load_attributes()
    if not spec:
        return _f("attribute-conflicts", SKIPPED, f"{ATTR_FILE} absent")
    rows, has_col = _attr_cells(root, reg)
    if rows is None or not has_col:
        return _f("attribute-conflicts", SKIPPED, "no Attribute column to read")
    claimed = {n for _, _, names, _ in rows for n in names}
    if len(claimed) < 2:
        return _f("attribute-conflicts", VERIFIED,
                  f"{len(claimed)} attribute(s) claimed - no pair to conflict")
    edges = {a["name"]: {c["target"]: c["why"]
                         for c in (a.get("conflicts_with") or [])}
             for a in spec["attributes"]}
    found, seen = [], set()
    for a in sorted(claimed):
        for b, why in (edges.get(a) or {}).items():
            if b in claimed and (b, a) not in seen:
                seen.add((a, b))
                found.append(f"{a} vs {b}: {why[:110]}")
    if found:
        return _f("attribute-conflicts", VERIFIED,
                  f"{len(found)} declared trade-off(s) between attributes this "
                  f"project demands - each is a cost already accepted, not a defect",
                  found[:6],
                  ["conflict-disposition reports which of these have a decision "
                   "record; this check only finds them"])
    return _f("attribute-conflicts", VERIFIED,
              f"{len(claimed)} attribute(s) claimed, no declared conflict between them")


def c_target_binds_criterion(root, state, reg):
    """A Measurable Target for an RSMR attribute should cite the criterion that
    defines how it is measured. Otherwise the number floats free of its method."""
    spec = load_attributes()
    if not spec:
        return _f("target-binds-criterion", SKIPPED, f"{ATTR_FILE} absent")
    rows, has_col = _attr_cells(root, reg)
    if rows is None or not has_col:
        return _f("target-binds-criterion", SKIPPED, "no Attribute column to read")
    valid = {c for a in spec["attributes"] for c in (a.get("criteria_esp32") or [])}
    measurable = {a["name"] for a in spec["attributes"] if a.get("criteria_esp32")}
    unbound, bogus = [], []
    for ln, rid, names, target in rows:
        cited = set(m.group(0) for m in RE_CRITERION.finditer(target))
        # A fabricated criterion id is a defect on ANY row. Testing caught this:
        # skipping rows whose attribute has no criteria let RL-ESP-99 through,
        # because the row was never examined at all.
        for c in cited:
            if "-ESP-" in c and c not in valid:
                bogus.append(f"{rid} line {ln}: cites {c}, which SECTION5 does "
                             f"not define")
        # A citation is only *required* where a measurable attribute is claimed.
        if any(n in measurable for n in names) and not cited:
            unbound.append(f"{rid} line {ln}: target cites no criterion id")
    if bogus:
        return _f("target-binds-criterion", REFUTED,
                  f"{len(bogus)} target(s) cite a criterion that does not exist",
                  bogus[:8])
    if unbound:
        return _f("target-binds-criterion", REFUTED,
                  f"{len(unbound)} target(s) for a measurable attribute cite no "
                  f"SECTION5 criterion, so the number has no stated method",
                  unbound[:8])
    return _f("target-binds-criterion", VERIFIED,
              "every target for a measurable attribute cites a real SECTION5 criterion")


def _claimed_conflicts(root, state, reg):
    """[(a, b, why)] - declared conflicts between attributes this project claims.

    Shared with c_attribute_conflicts so the two checks can never disagree about
    what the requirement set has bought into.
    """
    spec = load_attributes()
    if not spec:
        return None, f"{ATTR_FILE} absent"
    rows, has_col = _attr_cells(root, reg)
    if rows is None or not has_col:
        return None, "no Attribute column to read"
    claimed = {n for _, _, names, _ in rows for n in names}
    edges = {a["name"]: {c["target"]: c["why"]
                         for c in (a.get("conflicts_with") or [])}
             for a in spec["attributes"]}
    found, seen = [], set()
    for a in sorted(claimed):
        for b, why in (edges.get(a) or {}).items():
            if b in claimed and (b, a) not in seen:
                seen.add((a, b))
                found.append((a, b, why))
    return found, None


def _ad_records(root, reg):
    """[{id, path, text, decision, reason}] from the decision register."""
    pth = reg.get("decisions")
    if not pth:
        return None, "registers.decisions not set - there is nowhere to record a "\
                     "disposition"
    d = root / str(pth)
    if not d.is_dir():
        return None, f"{pth} is not a directory"
    out = []
    for fp in sorted(d.glob("*.md")):
        text = fp.read_text(encoding="utf-8", errors="ignore")

        def field(name):
            m = re.search(rf"^\s*{name}:\s*(.+?)\s*$", text, re.M)
            return m.group(1).strip() if m else ""

        out.append({"id": fp.stem.split("-")[0:3],
                    "name": fp.stem,
                    "path": fp.relative_to(root).as_posix(),
                    "text": text,
                    "decision": field("Decision"),
                    "reason": field("Technical reason")})
    return out, None


def _names_in(text, name):
    return re.search(rf"\b{re.escape(name)}\b", text, re.I) is not None


def c_conflict_disposition(root, state, reg):
    """A surfaced trade-off with no recorded decision is a question nobody answered.

    c_attribute_conflicts reports the conflicts this requirement set buys into,
    and reports them again every session, unchanged, forever. Nothing separates a
    trade-off the engineer weighed and settled from one they have never seen -
    and at a gate the reviewer is handed the conflicts without the decisions,
    when it is the decisions that deserve review.

    A disposition is an AD-S<n>-<NNN> record naming both attributes. That reuses
    the decision-record mechanism SECTION2 sec.3.1 already defines and
    c_decision_records already validates, rather than inventing a second place
    for the same kind of fact (invariant I5).

    Stage-scaled, because demanding a formal decision record for every trade-off
    at Prototype would contradict the enforcement ladder's own advisory posture
    at S1. From S2 an undisposed conflict is refuted.
    """
    conflicts, why = _claimed_conflicts(root, state, reg)
    if conflicts is None:
        return _f("conflict-disposition", SKIPPED, why)
    if not conflicts:
        return _f("conflict-disposition", VERIFIED,
                  "no declared conflict between the attributes this project "
                  "claims - nothing to dispose")

    ads, ad_why = _ad_records(root, reg)
    stage = ((state or {}).get("current") or {}).get("stage")

    if ads is None:
        return _f("conflict-disposition", SKIPPED,
                  f"{len(conflicts)} conflict(s) to dispose, but {ad_why}",
                  [f"{a} vs {b}" for a, b, _ in conflicts[:6]])

    disposed, weak, undisposed = [], [], []
    for a, b, cwhy in conflicts:
        hits = [ad for ad in ads
                if _names_in(ad["text"], a) and _names_in(ad["text"], b)]
        if not hits:
            undisposed.append(f"{a} vs {b}: no decision record names both - "
                              f"{cwhy[:90]}")
            continue
        ad = hits[0]
        argued = any(_names_in(ad["decision"] + " " + ad["reason"], n)
                     for n in (a, b))
        line = (f"{a} vs {b} -> {ad['name']}: "
                f"{(ad['decision'] or '(no Decision: line)')[:100]}")
        if argued:
            disposed.append(line)
        else:
            weak.append(f"{line}   [names both only outside Decision: and "
                        f"Technical reason: - check it is really about this "
                        f"trade-off]")

    n_ok = len(disposed) + len(weak)
    if undisposed and stage != "S1":
        return _f("conflict-disposition", REFUTED,
                  f"{len(undisposed)} of {len(conflicts)} declared trade-off(s) "
                  f"have no decision record naming both attributes - surfaced "
                  f"every session, answered in none",
                  undisposed[:8] + weak[:2],
                  [f"record one as AD-S<n>-<NNN>-<slug>.md under "
                   f"{reg.get('decisions')}, naming both attributes in "
                   f"Decision: or Technical reason:",
                   "this establishes that a decision exists and names the pair - "
                   "never that the decision resolves the conflict"])
    if undisposed:
        return _f("conflict-disposition", VERIFIED,
                  f"{len(undisposed)} of {len(conflicts)} trade-off(s) are not yet "
                  f"disposed. At S1 that is surfaced, not demanded - a Prototype "
                  f"is where trade-offs are still being discovered",
                  undisposed[:6] + disposed[:3],
                  ["from S2 an undisposed conflict is refuted"])
    return _f("conflict-disposition", VERIFIED,
              f"all {len(conflicts)} declared trade-off(s) carry a decision record "
              f"naming both attributes"
              + (f", {len(weak)} of them only outside the argued fields" if weak
                 else ""),
              disposed[:6] + weak[:3],
              ["a record exists and names the pair; whether it resolves the "
               "conflict is the engineer's judgement and stays that way"])


CHECKS = [c_registers_present, c_req_table_shape, c_decision_records,
          c_req_references_resolve, c_orphan_requirements,
          c_assumption_references,
          c_attribute_vocabulary, c_attribute_measurable,
          c_attribute_conflicts, c_target_binds_criterion,
          c_conflict_disposition]


def run(root: Path, state):
    reg = ((state or {}).get("current") or {}).get("registers") or {}
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(root, state, reg))
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
