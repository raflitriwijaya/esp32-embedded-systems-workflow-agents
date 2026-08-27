#!/usr/bin/env python3
"""ESP32 Stage Kernel - the agnostic core: is it pure, and is it tested?

Two questions that turn out to be one. SECTION3 sec.3.1 requires the core to
include no platform header; sec.4.1 says the core is the only thing a host
compiler can test; sec.4.3 measures coverage on the core alone. Break the seam
and the host build stops, and with it the coverage bar - silently, because the
firmware build keeps working.

That silence was verified rather than assumed. Building the reference project
both ways with esp_log.h inside the core and REQUIRES empty:

    ESP32 firmware -> Project build complete
    host tests     -> fatal error C1083: Cannot open include file: 'esp_log.h'

ESP-IDF grants every component 13 implicit dependencies, so its own mechanism
cannot enforce this. Only the host build can, and only if it runs.

WHAT THESE ESTABLISH
  purity      no platform coupling in the declared core sources
  reqs        no platform component declared beyond the implicit 13
  testable    a core exists at the stage that requires one
  coverage    the measured figure against the sec.4.3 bar, on core files only

WHAT THEY DO NOT
  That the core logic is right. That the tests assert anything worth asserting.
  Coverage is a proxy, and a weak one: 100% with no assertions still reads 100%.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

VERIFIED = "MACHINE_CHECKED"
REFUTED = "MACHINE_REFUTED"
SKIPPED = "UNVERIFIABLE"

STAGES = ["S1", "S2", "S3", "S4", "S5"]

# SECTION3 sec.4.3. (line %, branch % or None)
COVERAGE_BAR = {
    "S1": (None, None),
    "S2": (60.0, None),
    "S3": (80.0, None),
    "S4": (90.0, 80.0),
    "S5": (90.0, 80.0),
}
COVERAGE_NOTE = {
    "S1": "no coverage bar; one smoke test over the core API is sufficient",
    "S2": "60% line on the agnostic core; below it CI warns rather than fails",
    "S3": "80% line, and every error-return path exercised",
    "S4": "90% line and 80% branch",
    "S5": "90% maintained, and coverage must not decrease per change",
}

SRC_EXT = (".c", ".h", ".cpp", ".hpp", ".cc", ".cxx")


def _f(check, status, why, evidence=None, hints=None):
    return {"check": check, "status": status, "why": why,
            "evidence": evidence or [], "hints": hints or []}


def _rank(s):
    try:
        return STAGES.index(str(s).strip().upper())
    except ValueError:
        return None


def _core_files(root: Path, core_rel):
    d = root / str(core_rel)
    if not d.is_dir():
        return None
    return sorted(p for p in d.rglob("*")
                  if p.is_file() and p.suffix.lower() in SRC_EXT)


# --------------------------------------------------------------- purity

def c_core_purity(ctx):
    """The declared core, scanned as it stands rather than as it is written."""
    import guards
    root, core = ctx["root"], ctx["core"]
    if not core:
        return _f("core-purity", SKIPPED,
                  "current.registers.core is not set - nothing is declared as "
                  "the agnostic core")
    files = _core_files(root, core)
    if files is None:
        return _f("core-purity", SKIPPED, f"{core} is not a directory")
    if not files:
        return _f("core-purity", SKIPPED, f"{core} holds no C/C++ sources")
    gctx = {"core_dirs": [str(core).replace("\\", "/").rstrip("/")],
            "host_shims": ctx.get("host_shims") or []}
    bad = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for f in guards.g_core_purity(rel, text, gctx):
            bad.append(f"{rel}:{f.get('line')} {f['message'][:120]}")
    if bad:
        return _f("core-purity", REFUTED,
                  f"{len(bad)} platform coupling(s) in {len(files)} core "
                  f"source(s) - the host test build cannot compile these, and "
                  f"sec.4.3 measures coverage on what it compiles",
                  bad[:8])
    return _f("core-purity", VERIFIED,
              f"{len(files)} core source(s) carry no platform header, API, "
              f"Kconfig symbol or platform conditional",
              hints=["purity is what makes the host build possible; whether the "
                     "core logic is right is a different question"])


def c_core_explicit_reqs(ctx):
    """What the core declares beyond ESP-IDF's 13 implicit dependencies.

    An empty REQUIRES proves nothing - every component receives freertos, log,
    soc, hal and nine more without asking. What it can show is coupling the
    engineer added deliberately, which is the sec.2.2 `REQUIRES driver` trap.
    """
    root, core = ctx["root"], ctx["core"]
    if not core:
        return _f("core-explicit-reqs", SKIPPED, "no core declared")
    bd = ctx.get("build_dir")
    if not bd:
        return _f("core-explicit-reqs", SKIPPED,
                  "no build directory - ESP-IDF's component graph is written "
                  "there by a build")
    pd = Path(bd) / "project_description.json"
    if not pd.is_file():
        return _f("core-explicit-reqs", SKIPPED,
                  f"{Path(bd).name}/project_description.json absent")
    try:
        d = json.loads(pd.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError) as exc:
        return _f("core-explicit-reqs", SKIPPED, f"unreadable: {exc}")
    info = d.get("build_component_info") or {}
    common = set(d.get("common_component_reqs") or [])
    name = Path(str(core)).name
    entry = info.get(name)
    if not entry:
        return _f("core-explicit-reqs", SKIPPED,
                  f"no component named {name!r} in the build - the core is not "
                  f"registered as an ESP-IDF component, or is named differently")
    declared = []
    for key in ("reqs", "priv_reqs", "managed_reqs", "managed_priv_reqs"):
        declared += [(key, r) for r in (entry.get(key) or [])]
    extra = [(k, r) for k, r in declared if r not in common]
    if extra:
        return _f("core-explicit-reqs", REFUTED,
                  f"the core declares {len(extra)} component dependency(ies) "
                  f"beyond the {len(common)} every component receives - each is "
                  f"platform coupling added on purpose",
                  [f"{k}: {r}" for k, r in extra[:8]],
                  ["SECTION3 sec.2.2 step 5 writes `REQUIRES driver` on the core "
                   "with the comment \"no platform dependency\", which contradicts "
                   "itself - recorded as spec defect core-requires-driver"])
    return _f("core-explicit-reqs", VERIFIED,
              f"the core declares no component dependency beyond the "
              f"{len(common)} ESP-IDF grants implicitly",
              hints=[f"those {len(common)} still put esp_log.h and "
                     f"freertos/FreeRTOS.h on its include path, so this is not "
                     f"proof of purity - core-purity is"])


def c_host_testable_core(ctx):
    """sec.4.3 measures coverage on the agnostic core. No core, nothing to measure."""
    root, core, stage = ctx["root"], ctx["core"], ctx["stage"]
    here = _rank(stage)
    if here is None or here < 1:
        return _f("host-testable-core", SKIPPED,
                  f"{stage} sets no coverage bar, so a core is not yet required")
    if not core:
        return _f("host-testable-core", REFUTED,
                  f"{stage} requires {COVERAGE_NOTE[stage]}, and no agnostic "
                  f"core is declared - the bar cannot be met because there is "
                  f"nothing it could be measured on",
                  hints=["set current.registers.core, or state plainly that "
                         "this project has no host-testable logic"])
    files = _core_files(root, core)
    if not files:
        return _f("host-testable-core", REFUTED,
                  f"{core} is declared but holds no C/C++ sources; {stage} "
                  f"requires {COVERAGE_NOTE[stage]}")
    tests = ctx.get("host_tests")
    if not tests or not (root / str(tests)).is_dir():
        return _f("host-testable-core", REFUTED,
                  f"{len(files)} core source(s) exist but no host test "
                  f"directory is declared or present; sec.4.2 compiles the core "
                  f"on the host from {stage} onward",
                  hints=["set current.registers.host_tests"])
    return _f("host-testable-core", VERIFIED,
              f"{len(files)} core source(s) and a host test directory at "
              f"{tests} - the {stage} bar has something to be measured on")


# -------------------------------------------------------------- coverage

def _newest_coverage(root: Path, reports_dir="tests/reports"):
    d = root / reports_dir
    if not d.is_dir():
        return None
    cands = [p for p in d.glob("coverage-*.xml") if p.is_file()]
    if not cands:
        return None
    return max(cands, key=lambda f: f.stat().st_mtime)


def _parse_cobertura(path: Path):
    """{filename: (lines_covered, lines_valid, branch_rate)} plus the root rates."""
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return None
    per = {}
    for c in root.iter("class"):
        fn = (c.get("filename") or "").replace("\\", "/")
        if not fn:
            continue
        lines = list(c.iter("line"))
        cov = sum(1 for l in lines if int(l.get("hits", "0") or 0) > 0)
        try:
            br = float(c.get("branch-rate") or 0.0)
        except ValueError:
            br = 0.0
        got = per.get(fn, (0, 0, br))
        per[fn] = (got[0] + cov, got[1] + len(lines), br)
    return per


def c_coverage_bar(ctx):
    """SECTION3 sec.4.3, measured on the agnostic core alone."""
    root, core, stage = ctx["root"], ctx["core"], ctx["stage"]
    want_line, want_branch = COVERAGE_BAR.get(stage, (None, None))
    if want_line is None:
        return _f("coverage-bar", SKIPPED,
                  f"{stage}: {COVERAGE_NOTE.get(stage, 'no bar')}")
    if not core:
        return _f("coverage-bar", SKIPPED, "no core declared to measure")
    rep = _newest_coverage(root)
    if rep is None:
        return _f("coverage-bar", SKIPPED,
                  f"no tests/reports/coverage-*.xml; {stage} requires "
                  f"{COVERAGE_NOTE[stage]} and nothing has measured it",
                  hints=["a cobertura XML from any tool will do - sec.4.3 names "
                         "gcov/lcov, which do not work with MSVC"])
    per = _parse_cobertura(rep)
    if per is None:
        return _f("coverage-bar", SKIPPED, f"{rep.name} is not readable cobertura XML")

    # sec.4.3 measures the agnostic core ONLY. Test files inflate the figure -
    # in the reference project 99.3% overall against 100% for the core.
    core_abs = (root / str(core)).resolve().as_posix().lower()
    covered = valid = 0
    branches, included, excluded = [], [], []
    for fn, (cov, tot, br) in per.items():
        try:
            norm = Path(fn).resolve().as_posix().lower()
        except (OSError, ValueError):
            norm = fn.lower()
        if norm.startswith(core_abs):
            covered += cov
            valid += tot
            branches.append(br)
            included.append(f"{Path(fn).name}: {cov}/{tot} lines")
        else:
            excluded.append(Path(fn).name)

    # An empty measurement is not a passing one. A core with no covered lines,
    # or a report that touched no core file, would otherwise read as a bar met.
    if valid == 0:
        return _f("coverage-bar", REFUTED,
                  f"{rep.name} measures no line of the declared core, so the "
                  f"{stage} bar of {COVERAGE_NOTE[stage]} is not met - it is "
                  f"unmeasured. A report over zero lines is not 100%",
                  [f"excluded from the core: {', '.join(sorted(set(excluded))[:6])}"]
                  if excluded else [],
                  ["check that the host tests link the core, and that the "
                   "report was produced from that run"])

    # A report older than the sources describes code that no longer exists -
    # the same rule the build log carries.
    rep_m = rep.stat().st_mtime
    newer = [p.relative_to(root).as_posix() for p in (_core_files(root, core) or [])
             if p.stat().st_mtime > rep_m]
    line_pct = 100.0 * covered / valid
    branch_pct = 100.0 * (sum(branches) / len(branches)) if branches else None
    detail = (f"line {line_pct:.1f}%"
              + (f", branch {branch_pct:.1f}%" if branch_pct is not None else "")
              + f" over {valid} line(s) in {len(included)} core file(s)")

    if newer:
        return _f("coverage-bar", SKIPPED,
                  f"{rep.name} predates {len(newer)} core source(s), so it "
                  f"describes code that has since changed - {detail} is not "
                  f"evidence about the tree as it stands",
                  [f"newer than the report: {n}" for n in newer[:6]],
                  ["re-run the host tests under coverage"])

    fails = []
    if line_pct < want_line:
        fails.append(f"line coverage {line_pct:.1f}% is below the {stage} bar "
                     f"of {want_line:.0f}%")
    if want_branch is not None and branch_pct is not None \
            and branch_pct < want_branch:
        fails.append(f"branch coverage {branch_pct:.1f}% is below the {stage} "
                     f"bar of {want_branch:.0f}%")
    if fails:
        return _f("coverage-bar", REFUTED,
                  f"{len(fails)} coverage bar(s) unmet at {stage} - {detail}",
                  fails + included[:4],
                  [COVERAGE_NOTE[stage],
                   f"measured from {rep.relative_to(root).as_posix()}"])
    return _f("coverage-bar", VERIFIED,
              f"the {stage} bar is met - {detail}",
              included[:4],
              [COVERAGE_NOTE[stage],
               f"measured from {rep.relative_to(root).as_posix()}, "
               f"{len(set(excluded))} non-core file(s) excluded",
               "coverage counts lines reached, not assertions made - a suite "
               "that asserts nothing still measures 100%"])


CHECKS = [c_host_testable_core, c_core_purity, c_core_explicit_reqs,
          c_coverage_bar]


def run(root: Path, state, build_dir=None):
    cur = (state or {}).get("current") or {}
    reg = cur.get("registers") or {}
    ctx = {
        "root": root,
        "stage": cur.get("stage"),
        "core": reg.get("core"),
        "host_tests": reg.get("host_tests"),
        "host_shims": [root / str(reg["host_shims"])] if reg.get("host_shims") else [],
        "build_dir": build_dir,
    }
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
