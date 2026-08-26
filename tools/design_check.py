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


def _f(check, status, why, evidence=None):
    return {"check": check, "status": status, "why": why,
            "evidence": evidence or []}


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


CHECKS = [c_registers_present, c_req_table_shape, c_decision_records,
          c_req_references_resolve, c_orphan_requirements,
          c_assumption_references]


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
