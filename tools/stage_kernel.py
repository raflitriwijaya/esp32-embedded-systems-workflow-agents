#!/usr/bin/env python3
"""ESP32 Stage Kernel — layer 1.

Subcommands:
  detect   exit 0 if the directory is an ESP-IDF project, 1 otherwise
  cache    regenerate .stage-cache.json from ground truth
  check    run the stage-state.yaml consistency rules (SCHEMA §10)
  digest   emit the SessionStart digest on stdout (nothing if not an IDF project)

Design rules this file obeys (see ../README.md):
  I1  never writes stage-state.yaml
  I2  never invents a platform fact; every value carries a source, and
      anything unobservable is named in not_known rather than omitted
  I3  never emits PASS
  I4  unobserved quantities are surfaced so they become tier E3 assumptions

Spec: hooks/DIGEST_SPEC.md   State schema: ../STAGE_STATE_SCHEMA.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import guards
    import gates
    import design_check
    import rsmr
    import idfconfig
    import core_seam
except ImportError:  # guards.py sits beside this file
    sys.path.insert(0, str(Path(__file__).resolve().parent))
SCHEMA_VERSIONS_UNDERSTOOD = [1]
CACHE_NAME = ".stage-cache.json"
STATE_NAME = "stage-state.yaml"
SILENCE_NAME = ".no-stage-governance"
NL = chr(10)

# --- Stage model. Mirrors WORKFLOW_SECTION1 §2 and §4. -----------------------
# COUPLING: this table must be reviewed whenever SECTION1 §2 or §4 changes.
# The digest cites `spec:` so a reader can verify it against the source.
STAGES = {
    "S1": {"name": "Prototype",
           "bar": ["Robust"],
           "deferred": ["Scalable", "Maintainable", "Reliable"],
           "assumption_bar": None,            # unlimited (SECTION1 §4)
           "enforcement_default": "advisory"},
    "S2": {"name": "Functional Prototype",
           "bar": ["Robust", "Scalable"],
           "deferred": ["Maintainable", "Reliable"],
           "assumption_bar": 20,
           "enforcement_default": "guard"},
    "S3": {"name": "Pre-Production",
           "bar": ["Robust", "Scalable", "Maintainable"],
           "deferred": ["Reliable"],
           "assumption_bar": 5,
           "enforcement_default": "guard"},
    "S4": {"name": "Production-Ready",
           "bar": ["Robust", "Scalable", "Maintainable", "Reliable"],
           "deferred": [],
           "assumption_bar": 0,
           "enforcement_default": "strict"},
    "S5": {"name": "Field-Deployed Maintenance",
           "bar": ["Robust", "Scalable", "Maintainable", "Reliable"],
           "deferred": [],
           "assumption_bar": 0,
           "enforcement_default": "strict"},
}
NEXT_GATE = {"S1": "1->2", "S2": "2->3", "S3": "3->4", "S4": "4->5", "S5": "5->1"}

# The active-guard list is DERIVED from the guard registry, never written by
# hand here. A digest that advertises a guard which does not exist would be the
# same class of stale fact this whole framework exists to prevent (I2).

# --- Version-bound notes, keyed by installed ESP-IDF MAJOR.MINOR. ------------
# Deliberately fails loud: an IDF version with no entry produces a warning
# instead of stale notes. See DIGEST_SPEC "in_effect".
VERSION_NOTES = {
    "6.0": [
        "FreeRTOS stack depth is BYTES on ESP-IDF, not words",
        "I2C is driver/i2c_master.h; driver/i2c.h is EOL, removed in v7.0",
        "default compiler warnings are errors",
        "legacy ADC/DAC/I2S/Timer/PCNT/MCPWM/RMT/Sigma-Delta drivers are removed",
        "core dump format is ELF; CONFIG_ESP_COREDUMP_DATA_FORMAT_BIN is removed",
        "esp-mqtt, cJSON and network_provisioning live in the Component Registry",
    ],
}


# ============================================================ helpers

def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def short(h: str | None, n: int = 4) -> str | None:
    return h[:n] if h else None


def y(v) -> str:
    """Render a scalar as YAML."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{},&*?|<>=!%@`\"']", s) or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def ylist(items) -> str:
    return "[" + ", ".join(y(i) for i in items) + "]"


# ============================================================ detection

def detect_idf_project(root: Path) -> tuple[bool, str | None]:
    for name in ("sdkconfig", "sdkconfig.defaults", STATE_NAME):
        if (root / name).is_file():
            return True, name
    cml = root / "CMakeLists.txt"
    if cml.is_file():
        try:
            text = cml.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if "IDF_PATH" in text or "idf_component_register" in text:
            return True, "CMakeLists.txt IDF markers"
    for p in root.glob("sdkconfig.defaults.*"):
        if p.is_file():
            return True, p.name
    return False, None


# ============================================================ state file

class StateError(Exception):
    pass


def load_state(root: Path):
    """Return (state_dict, sha256) or raise StateError."""
    p = root / STATE_NAME
    if not p.is_file():
        raise StateError("NO_STATE")
    try:
        import yaml  # PyYAML
    except ImportError:
        raise StateError(
            "PyYAML is not available to this interpreter — install it into the "
            "interpreter that runs this hook (python -m pip install pyyaml)")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:                       # noqa: BLE001 - report verbatim
        raise StateError(f"{STATE_NAME} is not valid YAML: {exc}")
    if not isinstance(data, dict):
        raise StateError(f"{STATE_NAME} does not contain a mapping at the top level")
    return data, sha256_file(p)


# ============================================================ fold + consistency

def _as_aware(dt: datetime) -> datetime:
    """PyYAML yields naive datetimes for timestamps written without an offset.
    Comparing those against offset-aware ones raises. Assume local time."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _as_date(v):
    """Coerce a YAML scalar to a date. datetime is checked first because it
    subclasses date, and comparing datetime to date raises."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _events(state, kind):
    return [e for e in (state.get("log") or [])
            if isinstance(e, dict) and e.get("event") == kind]


def fold(state) -> dict:
    """Derive current-state facts from the append-only log."""
    opened = {e.get("id"): e for e in _events(state, "assumption_opened")}
    resolved = {e.get("id") for e in _events(state, "assumption_resolved")}
    open_ids = [i for i in opened if i not in resolved]

    today = date.today()
    past = []
    for i in open_ids:
        dl = _as_date(opened[i].get("deadline"))
        if dl and dl < today:
            past.append(i)

    origins = {}
    for i in open_ids:
        o = opened[i].get("origin") or "unspecified"
        origins[o] = origins.get(o, 0) + 1

    enf_events = [e for e in (state.get("log") or [])
                  if isinstance(e, dict)
                  and e.get("event") in ("enforcement_raised", "enforcement_lowered")]
    folded_enf = enf_events[-1].get("to") if enf_events else None

    last_stage = _events(state, "stage_entered")
    return {
        "assumptions_open": len(open_ids),
        "assumptions_past_deadline": len(past),
        "assumptions_by_origin": origins,
        "folded_enforcement": folded_enf,
        "last_stage_entered": last_stage[-1].get("stage") if last_stage else None,
    }


def _ts(e):
    v = e.get("ts")
    if isinstance(v, datetime):
        return _as_aware(v)
    if isinstance(v, date):
        return _as_aware(datetime(v.year, v.month, v.day))
    if isinstance(v, str):
        try:
            return _as_aware(datetime.fromisoformat(v))
        except ValueError:
            return None
    return None


# ================================================ log event required fields
#
# STAGE_STATE_SCHEMA.md sec.7 tabulates these, and nothing read the table. A
# hand-written table nothing enforces is a decoration, which is the same
# invariant-I2 failure the guard registry had against GUARD_SPEC.md.
#
# `owner` on assumption_opened is new here. SECTION1 asks for it three times -
# sec.3 Gate 1->2 "logged with owner + deadline", sec.4 "a logged assumption
# with an owner and a resolution deadline", and the Stage 2+ checklist "All
# open assumptions have owners and deadlines". The schema omitted it, so the
# gate check verified deadlines alone and reported the whole criterion
# MACHINE_CHECKED.
LOG_EVENT_FIELDS = {
    "stage_entered": ["stage", "by", "from"],
    "gate_decided": ["gate", "decision", "by", "unmet", "dossier"],
    "assumption_opened": ["id", "origin", "tier", "subject", "owner", "deadline"],
    "assumption_resolved": ["id", "resolution", "tier"],
    "attestation_made": ["id", "criterion", "by"],
    "attestation_superseded": ["id", "superseded_by", "reason"],
    "enforcement_raised": ["to", "by"],
    "enforcement_lowered": ["to", "by", "reason", "expires"],
    "waiver_granted": ["criterion", "reason", "expires", "by"],
    "target_added": ["target", "reason"],
    "target_removed": ["target", "reason"],
    "design_review_decided": ["outcome", "by", "unmet", "dossier"],
    "schema_migrated": ["from", "to"],
}
# `from: null` at project creation and `unmet: []` on a clean gate are both
# meaningful values. Presence of the key is what is required, not truthiness.
NULLABLE_FIELDS = {("stage_entered", "from"), ("gate_decided", "unmet"),
                   ("design_review_decided", "unmet")}


def _log_field_failures(state):
    out = []
    for n, e in enumerate(state.get("log") or [], 1):
        if not isinstance(e, dict):
            out.append(f"log entry {n} is not a mapping")
            continue
        ev = e.get("event")
        if not ev:
            out.append(f"log entry {n} has no event")
            continue
        req = LOG_EVENT_FIELDS.get(ev)
        if req is None:
            out.append(f"log entry {n}: event {ev!r} is not one the schema "
                       f"defines - a typo here silently drops the entry from "
                       f"every fold")
            continue
        for f in req:
            if f not in e:
                out.append(f"log entry {n} ({ev}) has no {f!r} - "
                           f"STAGE_STATE_SCHEMA.md sec.7 requires it")
            elif e[f] is None and (ev, f) not in NULLABLE_FIELDS:
                out.append(f"log entry {n} ({ev}) has {f}: null")
    return out


def check_consistency(state, folded, root: Path = Path(".")) -> list[str]:
    """SCHEMA §10. Returns a list of human-readable failures."""
    f = _log_field_failures(state)
    cur = state.get("current") or {}
    stage = cur.get("stage")

    if folded["last_stage_entered"] and stage != folded["last_stage_entered"]:
        f.append(f"current.stage = {stage} but the last stage_entered event "
                 f"says {folded['last_stage_entered']}")
    if not folded["last_stage_entered"]:
        f.append("no stage_entered event in log — the stage has no recorded origin")

    default = STAGES.get(stage, {}).get("enforcement_default")
    expected = folded["folded_enforcement"] or default
    if expected and cur.get("enforcement") != expected:
        f.append(f"current.enforcement = {cur.get('enforcement')} but folding the log "
                 f"gives {expected}")

    made = {e.get("id") for e in _events(state, "attestation_made")}
    for a in (state.get("attestations") or []):
        if not isinstance(a, dict):
            continue
        aid = a.get("id")
        if aid not in made:
            f.append(f"{aid} has no corresponding attestation_made log event")
        if a.get("method") == "agent-adversary":
            f.extend(_check_adversary(a))

    seen = None
    for e in (state.get("log") or []):
        if not isinstance(e, dict):
            continue
        t = _ts(e)
        if t is None:
            f.append(f"log entry {e.get('event')} has an unparseable ts")
            continue
        if seen and t < seen:
            f.append(f"log timestamps are not monotonic at {e.get('event')} {t.isoformat()}")
        seen = t

    opened = {e.get("id") for e in _events(state, "assumption_opened")}
    for e in _events(state, "assumption_resolved"):
        if e.get("id") not in opened:
            f.append(f"assumption_resolved {e.get('id')} was never assumption_opened")
        # Closure integrity (phase 5). Declaring an assumption 'measured' without
        # a measurement that exists on disk is the fabricated-evidence failure
        # (H6) arriving through the back door: the register would show a claim
        # closed by evidence that was never produced.
        if e.get("resolution") == "measured":
            ev = e.get("evidence")
            if not ev:
                f.append(f"{e.get('id')} resolved as 'measured' but cites no "
                         f"evidence file")
            elif not (root / ev).is_file() and not Path(ev).is_file():
                f.append(f"{e.get('id')} resolved as 'measured' but its evidence "
                         f"{ev} does not exist")

    sv = state.get("schema_version")
    if sv not in SCHEMA_VERSIONS_UNDERSTOOD:
        f.append(f"schema_version {sv} is not understood (understood: "
                 f"{SCHEMA_VERSIONS_UNDERSTOOD})")
    return f


def _check_adversary(a) -> list[str]:
    """SCHEMA §6.1 — validity conditions for method: agent-adversary."""
    out = []
    aid = a.get("id")
    d = a.get("dossier")
    if not d:
        out.append(f"{aid}: method agent-adversary but no dossier")
    elif not Path(d).is_file():
        out.append(f"{aid}: dossier not found at {d}")
    o = a.get("objections")
    if not isinstance(o, dict):
        out.append(f"{aid}: method agent-adversary but no objections counts")
    else:
        r, ac, rj = o.get("raised"), o.get("accepted"), o.get("rejected")
        if None in (r, ac, rj):
            out.append(f"{aid}: objections must state raised, accepted and rejected")
        elif ac + rj != r:
            out.append(f"{aid}: objections accepted+rejected ({ac}+{rj}) != raised ({r}) "
                       f"— every objection needs a disposition")
    return out


# ============================================================ ground truth

def _build_dirs(root: Path):
    out = []
    for d in sorted(root.glob("build*")):
        if d.is_dir() and (d / "project_description.json").is_file():
            out.append(d)
    return out


def parse_sdkconfig(p: Path) -> dict:
    cfg = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            m = re.match(r"^#\s*(CONFIG_\w+)\s+is not set$", line)
            if m:
                cfg[m.group(1)] = "n"
                continue
            m = re.match(r"^(CONFIG_\w+)=(.*)$", line)
            if m:
                v = m.group(2).strip()
                if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                    v = v[1:-1]
                cfg[m.group(1)] = v
    except OSError:
        pass
    return cfg


def resolve_idf(root: Path, bdirs):
    p = os.environ.get("IDF_PATH")
    if p and Path(p).is_dir():
        return Path(p), "IDF_PATH env"
    for bd in bdirs:
        try:
            data = json.loads((bd / "project_description.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ip = data.get("idf_path")
        if ip and Path(ip).is_dir():
            return Path(ip), f"{bd.name}/project_description.json:idf_path"
    return None, None


def read_idf_version(idf: Path):
    vt = idf / "version.txt"
    if vt.is_file():
        v = vt.read_text(encoding="utf-8", errors="ignore").strip().lstrip("v")
        if v:
            return v, "$IDF_PATH/version.txt"
    hdr = idf / "components" / "esp_common" / "include" / "esp_idf_version.h"
    if hdr.is_file():
        t = hdr.read_text(encoding="utf-8", errors="ignore")

        def g(k):
            m = re.search(rf"#define\s+{k}\s+\(?(\d+)\)?", t)
            return m.group(1) if m else None
        ma, mi, pa = (g("ESP_IDF_VERSION_MAJOR"), g("ESP_IDF_VERSION_MINOR"),
                      g("ESP_IDF_VERSION_PATCH"))
        if ma and mi:
            return f"{ma}.{mi}.{pa or '0'}", "$IDF_PATH/.../esp_idf_version.h"
    ev = os.environ.get("ESP_IDF_VERSION")
    if ev:
        return ev.lstrip("v"), "ESP_IDF_VERSION env"
    return None, None


# Files whose change invalidates a build log. A log describes the tree as it
# stood when it was written; anything here touched afterwards means it no
# longer describes the tree the engineer is about to be judged on.
BUILD_INPUT_EXT = (".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".S", ".s",
                   ".cmake", ".ld", ".csv", ".yml", ".yaml")
BUILD_INPUT_NAMES = ("CMakeLists.txt", "Kconfig", "Kconfig.projbuild",
                     "idf_component.yml", "sdkconfig", "sdkconfig.defaults")
# A ninja run that compiled nothing emits no warnings, which is not the same
# as compiling cleanly.
RE_COMPILED = re.compile(r"Building (?:C|CXX|ASM) object|\bCompiling\b", re.M)
RE_NO_WORK = re.compile(r"ninja: no work to do", re.I)


def _compiled_inputs(root: Path, build_dir: Path):
    """What the build system says it compiled, restricted to this project.

    CMake writes compile_commands.json into the build directory, and ESP-IDF
    ships it. Asking it is strictly better than inferring build inputs from file
    extensions: a host-compiled unit test under tests/ is a .c file and is not a
    firmware build input, and the sweep counted it. SECTION3 sec.4 requires those
    tests from Stage 2, so every test written knocked the zero-warning gate to
    UNVERIFIABLE - which teaches the engineer to ignore the staleness signal.

    Returns None when the list is unavailable, so the caller can fall back and
    say which method it used.
    """
    if not build_dir:
        return None, "no build directory"
    cc = build_dir / "compile_commands.json"
    if not cc.is_file():
        return None, f"{build_dir.name} holds no compile_commands.json"
    try:
        entries = json.loads(cc.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError) as exc:
        return None, f"{build_dir.name}/compile_commands.json is unreadable ({exc})"
    rr = root.resolve()
    out = set()
    for e in entries:
        f = e.get("file")
        if not f:
            continue
        p = Path(f)
        if not p.is_absolute():
            p = (Path(e.get("directory") or build_dir) / p)
        try:
            rel = p.resolve().relative_to(rr)
        except (ValueError, OSError):
            continue                       # ESP-IDF component, outside the project
        if rel.parts and rel.parts[0].lower().startswith("build"):
            continue                       # generated into the build tree
        out.add(p.resolve())
    if not out:
        # The file exists and lists nothing inside this tree. A build directory
        # copied from elsewhere carries absolute paths to where it was built,
        # and using it here would bind the log to another project's sources.
        return None, (f"{build_dir.name}/compile_commands.json lists no source "
                      f"inside this project - the build directory was most "
                      f"likely produced elsewhere and copied in")
    # Headers are not translation units, so compile_commands.json does not list
    # them. Take those sitting with the sources it does list.
    for d in {p.parent for p in list(out)}:
        for pat in ("*.h", "*.hpp", "include/**/*.h"):
            out.update(q.resolve() for q in d.glob(pat) if q.is_file())
    # A source added since the last cmake run is absent from the list, and its
    # absence is exactly what makes the build out of date. main/ and components/
    # are where ESP-IDF projects keep sources; tests/ is not one of them.
    for sub in ("main", "components"):
        d = root / sub
        if d.is_dir():
            for q in d.rglob("*"):
                if q.is_file() and q.suffix in (".c", ".h", ".cpp", ".hpp",
                                                ".cc", ".S", ".s"):
                    out.add(q.resolve())
    for name in BUILD_INPUT_NAMES + ("partitions.csv", "idf_component.yml",
                                     "dependencies.lock"):
        p = root / name
        if p.is_file():
            out.add(p.resolve())
    out.update(p.resolve() for p in root.glob("sdkconfig.defaults*")
               if p.is_file())
    return out, None


def _newest_build_input(root: Path, build_dir: Path = None):
    """(mtime, relative path, method) of the most recently touched build input."""
    listed, why = _compiled_inputs(root, build_dir)
    if listed is not None:
        method = f"{build_dir.name}/compile_commands.json"
        candidates = listed
    else:
        method = (f"a file sweep ({why}), so build inputs are inferred and "
                  f"unrelated files may register")
        candidates = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            segs = {s.lower() for s in p.relative_to(root).parts[:-1]}
            if segs & guards.SKIP_SEGMENTS or any(
                    s.startswith(guards.SKIP_PREFIXES) for s in segs):
                continue
            if p.name in BUILD_INPUT_NAMES or p.name.startswith("sdkconfig") \
                    or p.suffix in BUILD_INPUT_EXT:
                candidates.append(p)
    newest = None
    for p in candidates:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest[0]:
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                rel = p.as_posix()
            newest = (m, rel)
    return (newest[0], newest[1], method) if newest else None


def _build_log_evidence(root: Path, target: str, build_dir: Path = None):
    """Warning count from an archived build log, bound to the tree it describes.

    Evidence is a file on disk with a path and a timestamp, never a number the
    kernel inferred. That was true and it was not enough: a timestamp records
    when the LOG was written, not which source it describes. Editing a file
    after archiving a clean log left the gate reporting MACHINE_CHECKED "zero
    warnings" over code that would not compile - ESP-IDF v6.0 defaults to
    warnings-as-errors, so the introduced warning was a build failure.

    Two ways a log fails to establish anything, and both now return warnings as
    unknown rather than zero:

      stale  - a build input was touched after the log was written
      no-op  - the run compiled nothing, so it emitted no warnings either
    """
    d = root / "tests" / "reports"
    if not d.is_dir():
        return None
    cands = sorted(d.glob(f"build-{target}*.log"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not cands:
        return None
    log = cands[0]
    try:
        text = log.read_text(encoding="utf-8", errors="ignore")
        log_mtime = log.stat().st_mtime
    except OSError:
        return None

    ev = {
        "warnings": None,
        "errors": None,
        "source": log.relative_to(root).as_posix(),
        "at": datetime.fromtimestamp(log_mtime).astimezone()
              .isoformat(timespec="seconds"),
        "binding": None,
    }

    newest = _newest_build_input(root, build_dir)
    if newest and newest[0] > log_mtime:
        gap = newest[0] - log_mtime
        when = (f"{int(gap)}s" if gap < 60 else
                f"{int(gap // 60)}m" if gap < 3600 else
                f"{gap / 3600:.1f}h" if gap < 86400 else
                f"{gap / 86400:.1f}d")
        ev["binding"] = (
            f"STALE - {newest[1]} was modified {when} after this log was "
            f"written, so the log no longer describes the source tree. "
            f"Rebuild to re-establish it. Build inputs read from {newest[2]}")
        return ev
    if RE_NO_WORK.search(text) or not RE_COMPILED.search(text):
        ev["binding"] = (
            "NO-OP - this run compiled nothing, so its zero warnings say "
            "nothing about whether the source compiles cleanly. Build from "
            "clean, or touch the sources, to produce a log that does")
        return ev

    ev["warnings"] = len(re.findall(r"^.*?: warning: ", text, re.M))
    ev["errors"] = len(re.findall(r"^.*?: error: ", text, re.M))
    method = newest[2] if newest else "no build inputs found"
    ev["binding"] = (
        f"bound - no build input modified since the log was written; "
        f"{len(RE_COMPILED.findall(text))} translation unit(s) compiled. "
        f"Build inputs read from {method}")
    return ev


def _target_from_cfg(cfg, sdk_path, target, source):
    unicore = cfg.get("CONFIG_FREERTOS_UNICORE") == "y"
    hz = cfg.get("CONFIG_FREERTOS_HZ")
    hz = int(hz) if hz and hz.isdigit() else None
    psram = any(cfg.get(k) == "y" for k in ("CONFIG_SPIRAM", "CONFIG_ESP32_SPIRAM_SUPPORT",
                                            "CONFIG_ESP32S3_SPIRAM_SUPPORT"))
    return {
        "target": target,
        "configured": True,
        "cores": 1 if unicore else 2,
        "unicore": unicore,
        "freertos_hz": hz,
        "vtaskdelay_resolution_ms": (
            (1000 // hz) if (hz and 1000 % hz == 0) else
            (round(1000 / hz, 2) if hz else None)),
        "psram": psram,
        "warn_suppress_on": [k for k in ("CONFIG_COMPILER_DISABLE_DEFAULT_ERRORS",
                                         "CONFIG_COMPILER_DISABLE_GCC15_WARNINGS")
                             if cfg.get(k) == "y"],
        "sdkconfig": Path(sdk_path).as_posix(),
        "sdkconfig_sha256": sha256_file(sdk_path),
        "source": source,
    }


def collect_targets(root: Path, intent):
    found = {}
    for bd in _build_dirs(root):
        try:
            data = json.loads((bd / "project_description.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tgt = data.get("target")
        if not tgt:
            continue
        # project_description.json records config_file as an ABSOLUTE path to
        # wherever the build ran. Copy or clone a project with its build
        # directory intact and that path still points at the original machine,
        # so every fact derived from sdkconfig - unicore, tick rate, PSRAM,
        # warning suppression - would come from somebody else's project while
        # reading as if it were this one. Accept it only from inside this tree.
        cfgp = Path(data.get("config_file") or (root / "sdkconfig"))
        if not cfgp.is_absolute():
            cfgp = (root / cfgp).resolve()
        else:
            try:
                cfgp.resolve().relative_to(root.resolve())
            except ValueError:
                local = root / "sdkconfig"
                cfgp = local if local.is_file() else Path("")
        entry = (_target_from_cfg(parse_sdkconfig(cfgp), cfgp, tgt,
                                  f"{bd.name}/project_description.json")
                 if cfgp.is_file() else
                 {"target": tgt, "configured": True, "cores": None, "unicore": None,
                  "freertos_hz": None, "vtaskdelay_resolution_ms": None, "psram": None,
                  "warn_suppress_on": [], "sdkconfig": None, "sdkconfig_sha256": None,
                  "source": f"{bd.name}/project_description.json"})
        art = data.get("app_bin")
        if art:
            ap = Path(art)
            if not ap.is_absolute():
                ap = (bd / ap).resolve()
            if ap.is_file():
                rel = ap.relative_to(root) if root in ap.parents else ap
                entry["last_build"] = {
                    "artifact": rel.as_posix(),
                    "at": datetime.fromtimestamp(ap.stat().st_mtime).astimezone()
                          .isoformat(timespec="seconds"),
                    "warnings": None,           # not captured — see not_known
                }
        ev = _build_log_evidence(root, tgt, bd)
        if ev:
            lb = entry.setdefault("last_build", {"artifact": None, "at": None})
            lb["warnings"] = ev["warnings"]
            lb["errors"] = ev["errors"]
            lb["log"] = ev["source"]
            lb["log_binding"] = ev["binding"]
            lb["log_at"] = ev["at"]
        found[tgt] = entry

    root_sdk = root / "sdkconfig"
    if root_sdk.is_file():
        cfg = parse_sdkconfig(root_sdk)
        tgt = cfg.get("CONFIG_IDF_TARGET")
        if tgt and tgt not in found:
            found[tgt] = _target_from_cfg(cfg, root_sdk, tgt, "sdkconfig")

    # "Never built" and "the kernel is looking in the wrong directory" render
    # identically as configured:false, and an engineer whose stage-state.yaml
    # sits a level above the ESP-IDF project gets a permanently degraded agent
    # with nothing saying why. Name the second case when it is the likely one.
    misplaced = None
    if not found and not (root / "CMakeLists.txt").is_file():
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            if sub.name.startswith(".") or sub.name in guards.SKIP_SEGMENTS:
                continue
            ok, why = detect_idf_project(sub)
            if ok and why != STATE_NAME:
                misplaced = f"{sub.name}/ ({why})"
                break
    for t in (intent or []):
        if t not in found:
            note = "declared in intent but no build dir or sdkconfig found"
            if misplaced:
                note += (f" - an ESP-IDF project appears to be at {misplaced}, "
                         f"not at this root. The kernel reads sdkconfig, build "
                         f"dirs and tests/reports/ relative to the directory "
                         f"holding {STATE_NAME}, so move it there or run with "
                         f"-C pointing at the project")
            found[t] = {"target": t, "configured": False, "note": note}
    return [found[k] for k in sorted(found)]


# ============================================================ spec defects

SPEC_DEFECTS_FILE = "spec-defects.yaml"


def load_spec_defects():
    """The register of verified defects in the workflow specification itself.

    Guards check whether output is wrong. Nothing else checks whether output is
    *faithful to a defective example* - and the specification writes "stack 4096
    words" 43 lines below its own rule that ESP-IDF takes bytes. Copying that is
    obedience, and it is wrong.
    """
    f = Path(__file__).resolve().parent.parent / SPEC_DEFECTS_FILE
    if not f.is_file():
        return None
    try:
        import yaml
        return yaml.safe_load(f.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return None


def _register_sources(reg):
    """[(path, recorded_sha)] - every specification file the register covers.

    The register began covering SECTION2 alone. A defect found in SECTION5's
    Totals table then had nowhere to live that carried a hash, and an entry with
    no hash is exactly the stale-able fact invariant I2 forbids. Both spellings
    are honoured so existing registers keep working.
    """
    out = []
    if reg.get("source"):
        out.append((reg["source"], reg.get("spec_sha256")))
    for e in (reg.get("sources") or []):
        if isinstance(e, dict) and e.get("path"):
            out.append((e["path"], e.get("sha256")))
    return out


def spec_defect_status(reg):
    """Returns (stale_reason, defects) - stale_reason is None when trustworthy.

    A curated fact list is exactly what invariant I2 warns against, so the
    register carries the hash of the specification it was verified against. A
    changed specification makes it detectable rather than silently trusted."""
    if not reg:
        return "register absent", []
    reasons = []
    for src, recorded in _register_sources(reg):
        for base in (Path(__file__).resolve().parents[2], Path.cwd()):
            cand = base / src
            if cand.is_file():
                actual = sha256_file(cand)
                # Coerce: an all-digit hash written unquoted is parsed by YAML
                # as an integer, and an integer 0 is falsy - which would report
                # "not stamped" for a hash that is merely wrong. Found by test.
                recorded = str(recorded) if recorded is not None else None
                if not recorded or recorded == "None":
                    reasons.append(f"{src} baseline not stamped - run "
                                   f"'stage_kernel.py spec-stamp'")
                elif recorded != actual:
                    reasons.append(f"{src} changed since verification "
                                   f"({short(recorded)} -> {short(actual)}) - "
                                   f"re-verify")
                break
        else:
            reasons.append(f"{src} not reachable from here")
    return ("; ".join(reasons) or None), (reg.get("defects") or [])


def build_cache(root: Path, state, state_sha) -> dict:
    cur = (state or {}).get("current") or {}
    intent = (cur.get("intent") or {})
    bdirs = _build_dirs(root)
    idf, idf_src = resolve_idf(root, bdirs)
    ver, ver_src = read_idf_version(idf) if idf else (None, None)
    pinned = intent.get("idf_pinned")
    pinned = str(pinned) if pinned is not None else None

    targets = collect_targets(root, intent.get("targets"))

    unknowns = []
    for t in targets:
        if not t.get("configured"):
            unknowns.append(f"target_caps.{t['target']} (never configured or built)")
        elif t.get("freertos_hz") is None:
            unknowns.append(f"freertos_hz.{t['target']}")
        lb = t.get("last_build")
        if lb is None and t.get("configured"):
            unknowns.append(f"last_build.{t['target']}")
        elif lb and lb.get("warnings") is None:
            unknowns.append(f"build_warnings.{t['target']} "
                            f"(no tests/reports/build-{t['target']}*.log archived)")
    if ver is None:
        unknowns.append("idf_installed_version")

    folded = fold(state) if state else {}
    stage = cur.get("stage")
    bar = STAGES.get(stage, {}).get("assumption_bar")
    open_n = folded.get("assumptions_open")

    return {
        "generated_at": now_iso(),
        "generator_version": "1.0.0",
        "state_file_sha256": state_sha,
        "ground_truth": {
            "idf_installed": {"version": ver, "source": ver_src, "idf_path_source": idf_src},
            "idf_pinned": pinned,
            "idf_pin_match": (ver == pinned) if (ver and pinned) else None,
            "targets": targets,
        },
        "unknowns": unknowns,
        "derived": {
            "assumptions_open": open_n,
            "assumptions_past_deadline": folded.get("assumptions_past_deadline"),
            "assumptions_by_origin": folded.get("assumptions_by_origin"),
            "precision_bar": bar,
            "within_bar": None if (bar is None or open_n is None) else open_n <= bar,
        },
        "stale_if_changed": {
            "state_file_sha256": state_sha,
            "sdkconfig_sha256": {t["target"]: t.get("sdkconfig_sha256")
                                 for t in targets if t.get("configured")},
        },
    }


def cache_is_stale(root: Path, cache, state_sha) -> str | None:
    s = cache.get("stale_if_changed") or {}
    if s.get("state_file_sha256") != state_sha:
        return "stage-state.yaml changed since the cache was generated"
    for tgt, old in (s.get("sdkconfig_sha256") or {}).items():
        for t in (cache.get("ground_truth", {}).get("targets") or []):
            if t.get("target") == tgt and t.get("sdkconfig"):
                new = sha256_file(Path(t["sdkconfig"]))
                if new != old:
                    return (f"sdkconfig for {tgt} changed since generation "
                            f"({short(old)} -> {short(new)})")
    return None


# ============================================================ gate checklists

def spec_dir() -> Path | None:
    p = os.environ.get("EMBEDDED_WORKFLOW_SPEC_DIR")
    if p and Path(p).is_dir():
        return Path(p)
    here = Path(__file__).resolve()
    for anc in here.parents:
        cand = anc / "workflow-iot"
        if (cand / "WORKFLOW_SECTION1.md").is_file():
            return cand
    return None


def gate_criteria_total(gate: str):
    """Count '[ ]' items under the given gate heading in SECTION1 §3.

    Returns (count, source) or (None, None). Never guesses: if the spec is not
    reachable the digest reports null rather than a remembered number.
    """
    d = spec_dir()
    if not d:
        return None, None
    p = d / "WORKFLOW_SECTION1.md"
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    a, b = gate.split("->")
    pat = re.compile(rf"^###\s+Gate\s+{a}\s*(?:→|->)\s*{b}\b", re.M)
    m = pat.search(text)
    if not m:
        return None, None
    nxt = re.search(r"^###\s+", text[m.end():], re.M)
    block = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]
    n = len(re.findall(r"^\[ \]\s+\S", block, re.M))
    return (n or None), f"{p.name} §3"


# ============================================================ digest rendering

HEADER = ("# ESP32 STAGE KERNEL - context only. "
          "Do not summarise this block back to the user.")


def render_kernel_error(exc: BaseException) -> str:
    """Any unhandled defect in the kernel would otherwise reach the engineer as
    a missing digest - the one failure mode this whole design exists to prevent.
    Deliberately ASCII-only: this path may run in a degraded environment."""
    return NL.join([
        HEADER,
        f"kernel: {{ project: null, generated: {y(now_iso())}, "
        f"status: KERNEL_ERROR }}",
        f"error: {y(type(exc).__name__ + ': ' + str(exc))}",
        "stage: { id: null, rule: THE STAGE IS NOT ASSERTED - the kernel failed "
        "to render }",
        "note: this is a defect in the stage kernel, not in the project. Run "
        "'python tools/stage_kernel.py digest -C .' for the traceback.",
    ])


def render_no_state(root: Path, why: str, reason: str | None = None) -> str:
    L = [HEADER,
         f"kernel: {{ project: null, generated: {y(now_iso())}, status: NO_STATE }}",
         f"detected: {y('ESP-IDF project (' + why + ')')}",
         "stage: { id: null, rule: stage is UNKNOWN — apply no stage bar }"]
    if reason:
        L.append(f"blocked: {y(reason)}")
    L += ["bootstrap:",
          "  copy: esp32-embedded-systems-workflow-agents/templates/"
          "stage-state.template.yaml",
          f"  to: ./{STATE_NAME}",
          "  fill: [project.id, project.name, project.created, current.entered, "
          '"log[0].ts"]',
          f"  silence: create {SILENCE_NAME} to suppress this notice"]
    return "\n".join(L)


def render_platform(cache, stale_reason) -> list[str]:
    if cache is None:
        return ["platform:",
                "  cache: unavailable",
                "  reason: " + y("cache could not be generated"),
                "  rule: all platform facts are UNKNOWN — treat every platform "
                "claim as tier E3"]
    gt = cache.get("ground_truth", {})
    idf = gt.get("idf_installed", {})
    L = ["platform:"]
    if stale_reason:
        L += [f"  cache: stale",
              f"  stale_reason: {y(stale_reason)}",
              "  rule: platform facts below are UNVERIFIED — regenerate before "
              "relying on them"]
    else:
        L.append("  cache: fresh")
    L.append(f"  idf: {{ installed: {y(idf.get('version'))}, "
             f"pinned: {y(gt.get('idf_pinned'))}, "
             f"match: {y(gt.get('idf_pin_match'))}, "
             f"source: {y(idf.get('source'))} }}")
    if gt.get("idf_pin_match") is False:
        L.append("  pin_mismatch_rule: the documented pin and the installed toolchain "
                 "disagree — resolve before trusting any API-level claim")
    L.append("  targets:")
    for t in gt.get("targets") or []:
        if not t.get("configured"):
            L.append(f"    - {{ target: {y(t['target'])}, configured: false, "
                     f"note: {y(t.get('note'))} }}")
            continue
        L.append(f"    - {{ target: {y(t['target'])}, cores: {y(t.get('cores'))}, "
                 f"unicore: {y(t.get('unicore'))}, "
                 f"freertos_hz: {y(t.get('freertos_hz'))},")
        L.append(f"        vtaskdelay_resolution_ms: "
                 f"{y(t.get('vtaskdelay_resolution_ms'))}, "
                 f"psram: {y(t.get('psram'))},")
        lb = t.get("last_build")
        if not lb:
            lb_s = "null"
        else:
            lb_s = (f"{{ artifact: {y(lb.get('artifact'))}, "
                    f"at: {y(lb.get('at'))}, "
                    f"warnings: {y(lb.get('warnings'))}, "
                    f"errors: {y(lb.get('errors'))}, "
                    f"log: {y(lb.get('log'))} }}")
        L.append(f"        last_build: {lb_s} }}")
        # `warnings: null` beside a `log:` path reads like a failed read rather
        # than a log that does not describe this tree. Say which it is.
        if lb and lb.get("warnings") is None and lb.get("log_binding"):
            L.append(f"      # {t['target']}: warnings unknown - "
                     f"{lb['log_binding']}")
        if lb and lb.get("warnings"):
            L.append(f"      # WARNING {t['target']}: {lb['warnings']} compiler "
                     f"warning(s) in {lb.get('log')} - Gate 2->3 requires zero")
        if t.get("warn_suppress_on"):
            L.append(f"      # WARNING {t['target']}: warning suppression enabled "
                     f"({', '.join(t['warn_suppress_on'])}) — this is a gate finding")
    return L


def _rsmr_digest_lines(root: Path, state, stage):
    """What the stage obliges, and whether the record answers it.

    The count is the useful part at session start: 24 mandatory criteria at S3
    is not something an engineer carries in their head, and the digest is where
    it costs nothing to say.
    """
    obl = rsmr.obligations(stage)
    if obl is None:
        return []
    m, d, n = obl
    L = ["rsmr_obligations:   # SECTION5 sec.7.1",
         f"  mandatory: {len(m)}   # may not be deferred at this stage",
         f"  deferrable: {len(d)}   # each needs a DEBT with revisit_stage <= "
         f"the stage it becomes mandatory",
         f"  not_applicable: {len(n)}"]
    try:
        findings = rsmr.run(root, state)
    except Exception:                                  # noqa: BLE001
        return L + ["  record: null   # checks did not run"]
    su = rsmr.summarise(findings)
    ref = [f for f in findings if f["status"] == rsmr.REFUTED]
    skip = [f for f in findings if f["status"] == rsmr.SKIPPED]
    L.append(f"  record: {{ checked: {su['machine_checked']}, refuted: "
             f"{su['machine_refuted']}, unverifiable: {su['unverifiable']} }}")
    for f in ref[:4]:
        L.append(f"    - REFUTED {f['check']}: {f['why']}")
    if skip and not ref:
        L.append(f"    - not assessable yet: "
                 f"{', '.join(f['check'] for f in skip[:4])}")
    L.append("")
    return L


def render_digest(root: Path, state, state_sha, cache, stale_reason) -> str:
    cur = state.get("current") or {}
    stage = cur.get("stage")
    meta = STAGES.get(stage)
    folded = fold(state)
    failures = check_consistency(state, folded, root)

    sv = state.get("schema_version")
    project = (state.get("project") or {}).get("id")

    if sv not in SCHEMA_VERSIONS_UNDERSTOOD:
        return "\n".join([
            HEADER,
            f"kernel: {{ project: {y(project)}, generated: {y(now_iso())}, "
            f"status: UNKNOWN_SCHEMA }}",
            f"schema: {{ found: {y(sv)}, understood: {ylist(SCHEMA_VERSIONS_UNDERSTOOD)} }}",
            "stage: { id: null, rule: field semantics are not guessed — stage is "
            "NOT asserted }",
            *render_platform(cache, stale_reason)])

    if failures:
        L = [HEADER,
             f"kernel: {{ project: {y(project)}, generated: {y(now_iso())}, "
             f"status: INCONSISTENT }}",
             "stage:",
             "  id: null",
             "  rule: THE STAGE IS NOT ASSERTED — apply no stage bar until repaired",
             "  failures:"]
        L += [f"    - {y(f)}" for f in failures]
        L += render_platform(cache, stale_reason)
        L.append("note: platform facts remain valid — they come from ground truth, "
                 "not from stage-state.yaml")
        return "\n".join(L)

    gate = NEXT_GATE.get(stage)
    total, gsrc = gate_criteria_total(gate) if gate else (None, None)
    bar = meta["assumption_bar"]

    # phase 4: the validator now supplies real counts. Before it existed these
    # were null with a note; they are never faked as zero.
    gsum, gorph, grec = None, [], None
    try:
        gout = evaluate_gate(root, state, gate) if gate else None
        if gout:
            _rows, gsum, gorph = gout
            grec = gates.recommendation(gsum)
    except Exception:                              # noqa: BLE001
        gsum = None

    # A guard that exists is not the same as a guard that can check anything.
    # kconfig-exists needs a symbol table; without one it returns no findings for
    # every file, and listing it as active would present that silence as a clean
    # result. Ask the guards themselves rather than asserting from a static list.
    try:
        _gactive, _gdeg = guards.guard_status(cur.get("enforcement"),
                                              _guard_context(root, state))
    except Exception:                              # noqa: BLE001
        _gactive, _gdeg = guards.implemented_guards(cur.get("enforcement")), []
    _gdormant = [d for d in _gdeg if d[1] == "dormant"]
    _gpartial = [d for d in _gdeg if d[1] == "partial"]

    L = [HEADER,
         f"kernel: {{ project: {y(project)}, generated: {y(now_iso())}, status: OK }}",
         "",
         "stage:",
         f"  id: {y(stage)}",
         f"  name: {y(meta['name'])}",
         f"  rsmr_bar: {ylist(meta['bar'])}",
         f"  deferred: {ylist(meta['deferred'])}   # design intent, not enforced",
         f"  entered: {y(cur.get('entered'))}",
         "  spec: SECTION1 §2",
         "",
         "enforcement:",
         f"  level: {y(cur.get('enforcement'))}",
         f"  is_stage_default: "
         f"{y(cur.get('enforcement') == meta['enforcement_default'])}",
         "  overridable_by_engineer: true",
         f"  active_guards: {ylist(_gactive)}",
         *([] if not _gdormant else
           ["  dormant_guards:   # implemented, but cannot check anything here",
            *[f"    - {n}: {r}" for n, _kind, r in _gdormant]]),
         *([] if not _gpartial else
           ["  partial_guards:   # running, with a named blind spot",
            *[f"    - {n}: {r}" for n, _kind, r in _gpartial]]),
         "  guards_denying: " + ("false   # advisory: findings are surfaced as "
                                 "context, never denied"
                                 if cur.get('enforcement') == 'advisory' else "true"),
         "",
         *_rsmr_digest_lines(root, state, stage),
         "next_gate:",
         f"  gate: {y(gate)}",
         f"  spec: {y(gsrc or 'SECTION1 §3 (not reachable from here)')}",
         (f"  criteria: {{ total: {y((gsum or {}).get('total', total))}, "
          f"machine_checked: {y((gsum or {}).get('machine_checked'))}, "
          f"machine_refuted: {y((gsum or {}).get('machine_refuted'))}, "
          f"human_attested: {y((gsum or {}).get('human_attested'))}, "
          f"unverifiable: {y((gsum or {}).get('unverifiable'))} }}"
          if gsum else
          f"  criteria: {{ total: {y(total)}, machine_checked: null, "
          f"machine_refuted: null, human_attested: null, unverifiable: null }}"),
         (f"  recommendation: {y(grec[0])}   # {grec[1]}" if grec else
          "  recommendation: null   # validator could not run (SECTION1 unreachable)"),
         "  rule: a criterion that is not shown as met must never be described as "
         "met; READY is a recommendation and the gate decision is the "
         "engineer's to record",
         *( [f"  anchors_lost: {ylist(gorph)}   # SECTION1 wording may have changed"]
            if gorph else [] ),
         "",
         "assumptions:",
         f"  open: {y(folded['assumptions_open'])}",
         f"  bar: {y(bar)}   # SECTION1 §4 precision bar for {stage}",
         f"  within_bar: {y(None if bar is None else folded['assumptions_open'] <= bar)}",
         f"  past_deadline: {y(folded['assumptions_past_deadline'])}",
         f"  by_origin: {{ " + ", ".join(
             f"{k}: {v}" for k, v in sorted(
                 (folded.get('assumptions_by_origin') or {}).items())) + " }",
         f"  register: {y(((cur.get('registers') or {}).get('assumptions')))}",
         ""]
    L += render_platform(cache, stale_reason)
    L.append("")

    ver = ((cache or {}).get("ground_truth", {}).get("idf_installed", {}) or {}).get("version")
    mm = ".".join(ver.split(".")[:2]) if ver else None
    notes = VERSION_NOTES.get(mm) if mm else None
    if notes:
        L.append("in_effect:")
        L.append(f"  bound_to: {y('IDF ' + mm)}")
        L.append("  notes:")
        L += [f"    - {y(n)}" for n in notes]
    else:
        L.append("in_effect:")
        L.append(f"  bound_to: null   # no version-bound notes for IDF {mm or 'unknown'}")
        L.append("  rule: verify API semantics against the Documentation MCP before "
                 "asserting them")
    L.append("")

    reg_defects = load_spec_defects()
    stale_reason, defects = spec_defect_status(reg_defects)
    if defects or stale_reason:
        traps = [d for d in defects if d.get("digest")]
        L.append("spec_defects:")
        L.append(f"  source: {y((reg_defects or {}).get('source'))}")
        L.append(f"  verified_on: {y((reg_defects or {}).get('verified_on'))}")
        L.append(f"  total: {len(defects)}   # {len(traps)} shown; rest in "
                 f"{SPEC_DEFECTS_FILE}")
        if stale_reason:
            L.append(f"  STALE: {y(stale_reason)}")
        if traps:
            L.append("  traps:   # copying the spec faithfully here produces "
                     "wrong output")
            for d in traps:
                lines = ",".join(str(x) for x in (d.get("lines") or [])[:4])
                # First sentence only: enough to recognise the trap while
                # reading, with the full correction one file away.
                corr = " ".join(str(d.get("correct", "")).split())
                head = corr.split(". ")[0]
                if len(head) > 150:
                    head = head[:147] + "..."
                elif head != corr:
                    head += "."
                L.append(f"    - at: {y('L' + lines)}")
                L.append(f"      says: {y(d.get('says'))}")
                L.append(f"      correct: {y(head)}")
        L.append("  rule: do not reproduce these, even when copying the "
                 "specification's own example")
        L.append("")

    # Section 2 design artifacts - shape only
    try:
        dfind = design_check.run(root, state)
        dsum = design_check.summarise(dfind)
    except Exception:                                  # noqa: BLE001
        dfind, dsum = [], None
    if dsum and (dsum["machine_refuted"] or dsum["machine_checked"]):
        L.append("design:")
        L.append(f"  checks: {{ machine_checked: {dsum['machine_checked']}, "
                 f"machine_refuted: {dsum['machine_refuted']}, "
                 f"unverifiable: {dsum['unverifiable']} }}")
        ref = [f for f in dfind if f["status"] == design_check.REFUTED]
        if ref:
            L.append("  refuted:")
            for f in ref[:4]:
                L.append(f"    - {y(f['check'] + ': ' + f['why'])}")
        L.append("  rule: these establish SHAPE only - never whether a "
                 "requirement is right or a target is true")
        # Quality attributes: what the project claims to be buying, and what of
        # that no criterion anywhere can settle.
        try:
            aspec = design_check.load_attributes()
            reg = ((state.get("current") or {}).get("registers") or {})
            arows, has_col = design_check._attr_cells(root, reg)
        except Exception:                              # noqa: BLE001
            aspec, arows, has_col = None, None, False
        if aspec and arows and has_col:
            crit = {a["name"]: (a.get("criteria_esp32") or [])
                    for a in aspec["attributes"]}
            claimed = sorted({n for _, _, names, _ in arows for n in names})
            unmeas = [n for n in claimed if not crit.get(n)]
            L.append("  attributes:")
            L.append(f"    claimed: {ylist(claimed)}")
            if unmeas:
                L.append(f"    no_measurable_criteria: {ylist(unmeas)}")
                L.append("    rule: a requirement resting only on these is "
                         "UNVERIFIABLE by construction - SECTION5 covers RSMR only")
        L.append("")

    unknowns = (cache or {}).get("unknowns") or []
    L.append("not_known:")
    L.append(f"  items: {ylist(unknowns) if unknowns else '[]'}")
    L.append("  rule: any claim in these areas is tier E3 — open an ASM, never state "
             "it as fact")
    L.append("")
    L.append("source:")
    L.append(f"  state_file: {y(STATE_NAME + '@' + (short(state_sha) or '?'))}")
    L.append(f"  cache: {{ file: {y(CACHE_NAME)}, "
             f"generated: {y((cache or {}).get('generated_at'))} }}")
    L.append("  authority: { gates: SECTION1 §3, schema: STAGE_STATE_SCHEMA.md }")
    return "\n".join(L)


# ============================================================ commands

def load_cache(root: Path):
    p = root / CACHE_NAME
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_detect(root: Path) -> int:
    ok, why = detect_idf_project(root)
    if ok:
        print(why)
    return 0 if ok else 1


def cmd_cache(root: Path) -> int:
    try:
        state, sha = load_state(root)
    except StateError as e:
        print(f"cannot generate cache: {e}", file=sys.stderr)
        return 1
    cache = build_cache(root, state, sha)
    (root / CACHE_NAME).write_text(json.dumps(cache, indent=2, default=str),
                                   encoding="utf-8")
    print(f"wrote {CACHE_NAME}")
    return 0


def cmd_check(root: Path) -> int:
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(str(e), file=sys.stderr)
        return 2
    failures = check_consistency(state, fold(state), root)
    if not failures:
        print("stage-state.yaml is consistent")
        return 0
    for f in failures:
        print(f"FAIL {f}")
    return 1


def cmd_digest(root: Path) -> int:
    ok, why = detect_idf_project(root)
    if not ok or (root / SILENCE_NAME).exists():
        return 0                                   # condition A: emit nothing

    try:
        state, sha = load_state(root)
    except StateError as e:
        if str(e) == "NO_STATE":
            print(render_no_state(root, why))
        else:
            print(render_no_state(root, why, reason=str(e)))
        return 0

    try:
        cache = load_cache(root)
    except Exception:                              # noqa: BLE001
        cache = None
    stale = None
    if cache is None:
        try:
            cache = build_cache(root, state, sha)
            (root / CACHE_NAME).write_text(json.dumps(cache, indent=2, default=str),
                                           encoding="utf-8")
        except Exception:                          # noqa: BLE001 - never break a session
            cache = None
    else:
        stale = cache_is_stale(root, cache, sha)
        if stale:
            try:
                cache = build_cache(root, state, sha)
                (root / CACHE_NAME).write_text(json.dumps(cache, indent=2, default=str),
                                               encoding="utf-8")
                stale = None
            except Exception:                      # noqa: BLE001
                pass
    try:
        print(render_digest(root, state, sha, cache, stale))
    except Exception as exc:                       # noqa: BLE001 - see docstring
        print(render_kernel_error(exc))
        if os.environ.get("STAGE_KERNEL_DEBUG"):
            raise
    return 0


def _guard_context(root: Path, state):
    """Build the guard context from ground truth, not from the state file.

    kconfig symbols are read live from sdkconfig rather than from the cache: the
    cache may lag a menuconfig run by a session, and a stale symbol table would
    produce false denials - the failure mode most likely to make an engineer
    switch the guards off.
    """
    cache = load_cache(root) or {}
    gt = cache.get("ground_truth", {})
    targets = gt.get("targets") or []
    syms = set()
    for t in targets:
        sk = t.get("sdkconfig")
        if sk and Path(sk).is_file():
            syms |= set(parse_sdkconfig(Path(sk)))
    if not syms:
        rs = root / "sdkconfig"
        if rs.is_file():
            syms = set(parse_sdkconfig(rs))
    reg = ((state or {}).get("current") or {}).get("registers") or {}
    core = reg.get("core")
    cores = [str(core).replace("\\", "/").rstrip("/")] if core else []
    shim = reg.get("host_shims")
    shims = [root / str(shim)] if shim else []
    return {
        "targets": targets,
        "kconfig_symbols": syms or None,
        "idf_version": (gt.get("idf_installed") or {}).get("version"),
        # The seam guard needs to know what counts as core. Absent a
        # declaration it stays silent rather than guessing which directory the
        # engineer meant - the same posture as every other unmet precondition.
        "core_dirs": cores,
        "host_shims": shims,
        "root": root,
    }


def _render_findings(items) -> str:
    lines = []
    for f in items:
        loc = f":{f['line']}" if f.get("line") else ""
        lines.append(f"[{f['guard']}]{loc} {f['message']}  (rule: {f['cite']})")
    return NL.join(lines)


def cmd_guard(root: Path) -> int:
    """PreToolUse guard. Reads the hook event on stdin, decides, exits 0."""
    raw = sys.stdin.read() or ""
    raw = raw.lstrip("﻿").strip()            # PowerShell writes a UTF-8 BOM
    if not raw:
        return 0
    try:
        ev = json.loads(raw)
    except ValueError as exc:
        # Silence here would be indistinguishable from "checked and clean" - the
        # exact failure this framework refuses. Say the file was not checked.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"[stage-kernel] guard could not parse the hook "
                                 f"payload ({exc}). This file was NOT checked - do "
                                 f"not read the absence of findings as clean."}}))
        return 0
    ti = ev.get("tool_input") or {}

    # The tool schema and the hooks documentation disagree on these key names,
    # so probe both instead of trusting either. A guard that silently never
    # fires is worse than no guard.
    path = (ti.get("file_path") or ti.get("path")
            or ti.get("notebook_path") or "")
    texts = [v for k in ("content", "file_text", "new_string", "new_source")
             for v in [ti.get(k)] if isinstance(v, str)]
    for e in (ti.get("edits") or []):
        if isinstance(e, dict) and isinstance(e.get("new_string"), str):
            texts.append(e["new_string"])
    text = NL.join(texts)
    if not path or not text:
        return 0

    cwd = ev.get("cwd")
    if cwd and Path(cwd).is_dir():
        root = Path(cwd).resolve()
    ok, _ = detect_idf_project(root)
    if not ok or (root / SILENCE_NAME).exists():
        return 0

    enforcement = "advisory"
    try:
        state, _sha = load_state(root)
        enforcement = ((state.get("current") or {}).get("enforcement")
                       or "advisory")
    except StateError:
        state = None                       # no governance yet: warn, never deny

    try:
        findings = guards.run(path, text, _guard_context(root, state))
    except Exception as exc:               # noqa: BLE001
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"[stage-kernel] guards failed to run "
                                 f"({type(exc).__name__}: {exc}). This file was "
                                 f"NOT checked - do not read the absence of "
                                 f"findings as a clean result."}}))
        return 0
    if not findings:
        return 0

    deny, warn = guards.decide(findings, enforcement)
    out = {"hookEventName": "PreToolUse"}
    if deny:
        out["permissionDecision"] = "deny"
        out["permissionDecisionReason"] = (
            f"stage-kernel guards ({enforcement}) blocked this write:" + NL
            + _render_findings(deny)
            + NL + NL + "Fix the finding, or approve this call to override - an "
              "override is a decision you are recording, not a bypass.")
    if warn:
        out["additionalContext"] = (
            f"[stage-kernel] advisory findings (not blocking at "
            f"enforcement={enforcement}):" + NL + _render_findings(warn))
    print(json.dumps({"hookSpecificOutput": out}))
    return 0


def _gate_context(root: Path, state):
    # The gate verdict must not depend on whether someone happened to run
    # `cache` first. Build it on demand, exactly as the digest does.
    cache = load_cache(root)
    if cache is None:
        try:
            _s, sha = load_state(root)
            cache = build_cache(root, _s, sha)
            (root / CACHE_NAME).write_text(json.dumps(cache, indent=2, default=str),
                                           encoding="utf-8")
        except Exception:                          # noqa: BLE001
            cache = {}
    cache = cache or {}
    gt = cache.get("ground_truth", {})
    # Read the symbol table here too. It was hardcoded to None, which no current
    # gate check happens to consult - but a future one would have been silently
    # dormant with nothing to reveal it, which is how the kconfig-exists hole
    # opened in the first place.
    syms = set()
    for t in (gt.get("targets") or []):
        sk = t.get("sdkconfig")
        if sk and Path(sk).is_file():
            syms |= set(parse_sdkconfig(Path(sk)))
    if not syms and (root / "sdkconfig").is_file():
        syms = set(parse_sdkconfig(root / "sdkconfig"))
    return {"root": root, "state": state,
            "targets": gt.get("targets") or [],
            "idf_version": (gt.get("idf_installed") or {}).get("version"),
            "kconfig_symbols": syms or None}


def evaluate_gate(root: Path, state, gate: str):
    """Returns (rows, summary, orphans) or None when the spec is unreachable."""
    d = spec_dir()
    if not d:
        return None
    criteria = gates.parse_criteria(d, gate)
    if not criteria:
        return None
    rows, orphans = gates.evaluate(gate, criteria, _gate_context(root, state),
                                   state.get("attestations") if state else [])
    return rows, gates.summarise(rows), orphans


def cmd_gate(root: Path) -> int:
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(f"cannot evaluate a gate: {e}", file=sys.stderr)
        return 2
    stage = (state.get("current") or {}).get("stage")
    gate = os.environ.get("STAGE_KERNEL_GATE") or NEXT_GATE.get(stage)
    out = evaluate_gate(root, state, gate)
    if out is None:
        print(f"gate {gate}: SECTION1 not reachable - cannot evaluate",
              file=sys.stderr)
        return 2
    rows, summary, orphans = out
    rec, why = gates.recommendation(summary)
    print(f"GATE {gate}   RECOMMENDATION: {rec}  ({why})")
    print(f"  total {summary['total']} | machine-checked "
          f"{summary['machine_checked']} | refuted {summary['machine_refuted']} "
          f"| attested {summary['human_attested']} | unverifiable "
          f"{summary['unverifiable']}")
    if orphans:
        print(f"  ! checks whose anchor matched nothing: {', '.join(orphans)} "
              f"- SECTION1 wording may have changed")
    for i, r in enumerate(rows, 1):
        print(NL + f"  {i}. [{r['status']}] {r['criterion']}")
        print(f"     {r['why']}")
        for e in r["evidence"][:6]:
            print(f"     evidence: {e}")
        for h in r["hints"][:3]:
            print(f"     hint: {h}")
    print(NL + "  This is a recommendation, not a decision. The decision is "
          "recorded by the engineer as a gate_decided log event.")
    return 0


def cmd_design(root: Path) -> int:
    """Run the Section 2 design artifact shape checks."""
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(f"cannot run design checks: {e}", file=sys.stderr)
        return 2
    findings = design_check.run(root, state)
    summary = design_check.summarise(findings)
    print(f"DESIGN SHAPE CHECKS   machine-checked {summary['machine_checked']} "
          f"| refuted {summary['machine_refuted']} | unverifiable "
          f"{summary['unverifiable']}")
    for f in findings:
        print(NL + f"  [{f['status']}] {f['check']}")
        print(f"     {f['why']}")
        for e in f["evidence"][:6]:
            print(f"     evidence: {e}")
        for h in f.get("hints") or []:
            print(f"     hint: {h}")
    print(NL + "  These establish SHAPE only. A well-formed requirement can "
          "still be the wrong requirement.")
    return 0


def cmd_spec_stamp(root: Path) -> int:
    """Record the hash of the specification the defect register was verified against."""
    reg_path = Path(__file__).resolve().parent.parent / SPEC_DEFECTS_FILE
    reg = load_spec_defects()
    if not reg:
        print(f"{SPEC_DEFECTS_FILE} not found or unreadable", file=sys.stderr)
        return 2
    text = reg_path.read_text(encoding="utf-8")
    done, missing = [], []
    for src, _rec in _register_sources(reg):
        spec = None
        for base in (Path(__file__).resolve().parents[2], Path.cwd()):
            if (base / src).is_file():
                spec = base / src
                break
        if spec is None:
            missing.append(src)
            continue
        digest_hex = sha256_file(spec)
        if src == reg.get("source"):
            text = re.sub(r"^spec_sha256:.*$", f"spec_sha256: {digest_hex}",
                          text, count=1, flags=re.M)
        else:
            # Stamp the sha that follows this path inside the `sources:` list.
            pat = (r"(-\s+path:\s*" + re.escape(src) + r"\s*\n\s+sha256:)[^\n]*")
            text, n = re.subn(pat, r"\1 " + digest_hex, text, count=1)
            if not n:
                missing.append(f"{src} (no sha256 line to stamp)")
                continue
        done.append(f"{src} ({short(digest_hex)})")
    if not done:
        print("nothing stamped: " + ", ".join(missing or ["no sources listed"]),
              file=sys.stderr)
        return 2
    reg_path.write_text(text, encoding="utf-8")
    print(f"stamped {SPEC_DEFECTS_FILE} against:")
    for d in done:
        print(f"  {d}")
    for m in missing:
        print(f"  ! not reachable: {m}", file=sys.stderr)
    return 0


def cmd_rsmr(root: Path) -> int:
    """SECTION5 sec.7 - what this stage obliges, and whether the record answers it."""
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(f"cannot apply the RSMR matrix: {e}", file=sys.stderr)
        return 2
    stage = (state.get("current") or {}).get("stage")
    findings = rsmr.run(root, state)
    summary = rsmr.summarise(findings)
    obl = rsmr.obligations(stage)
    print(f"RSMR x STAGE   stage {stage}")
    if obl:
        m, d, n = obl
        print(f"  obligations: {len(m)} mandatory | {len(d)} deferrable | "
              f"{len(n)} not applicable   (SECTION5 sec.7.1)")
    print(f"  checks: machine-checked {summary['machine_checked']} | refuted "
          f"{summary['machine_refuted']} | unverifiable {summary['unverifiable']}")
    for f in findings:
        print(NL + f"  [{f['status']}] {f['check']}")
        print(f"     {f['why']}")
        for e in f["evidence"][:8]:
            print(f"     - {e}")
        for h in f["hints"][:3]:
            print(f"     hint: {h}")
    print(NL + "  These establish that the assessment is complete and its "
          "deferrals valid.")
    print("  Not one of them establishes that a criterion is actually met - "
          "that is the")
    print("  engineer's assessment, and it stays that way.")
    return 0


def cmd_rsmr_scorecard(root: Path) -> int:
    """Write a scorecard whose rows are exactly this stage's criteria."""
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(f"cannot generate a scorecard: {e}", file=sys.stderr)
        return 2
    cur = state.get("current") or {}
    stage = cur.get("stage")
    text = rsmr.render_scorecard(stage, (state.get("project") or {}).get("id"))
    if text is None:
        print(f"stage {stage!r} is not one of {', '.join(rsmr.STAGES)}",
              file=sys.stderr)
        return 2
    reg = cur.get("registers") or {}
    rel = reg.get("rsmr_scorecard") or f"quality/rsmr-scorecard-{stage}.md"
    out = root / str(rel)
    if out.exists():
        # Overwriting would destroy recorded verdicts, which are the engineer's
        # own assessment and not something this tool may discard.
        print(f"{rel} already exists - not overwritten. Delete it, or point "
              f"current.registers.rsmr_scorecard elsewhere.", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    m, d, n = rsmr.obligations(stage)
    print(f"wrote {rel}")
    print(f"  {len(m)} mandatory + {len(d)} deferrable rows to fill, "
          f"{len(n)} listed as not applicable at {stage}")
    if not reg.get("rsmr_scorecard"):
        print(NL + "  Set this in stage-state.yaml so the checks can find it:")
        print(f"    current.registers.rsmr_scorecard: {rel}")
    return 0


def cmd_config(root: Path) -> int:
    """SECTION3 sec.2.2 - sdkconfig and partition table against the stage bar."""
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(f"cannot apply the config bar: {e}", file=sys.stderr)
        return 2
    # Build on demand rather than reading whatever happens to be there. The
    # verdict must not depend on someone having run `cache` first - the gate
    # context does the same, for the same reason.
    targets = _gate_context(root, state).get("targets") or []
    pkg = Path(__file__).resolve().parent.parent
    findings = idfconfig.run(root, state, targets, pkg)
    su = idfconfig.summarise(findings)
    stage = (state.get("current") or {}).get("stage")
    print(f"IDF CONFIG vs STAGE BAR   stage {stage}")
    print(f"  machine-checked {su['machine_checked']} | refuted "
          f"{su['machine_refuted']} | unverifiable {su['unverifiable']}")
    for f in findings:
        print(NL + f"  [{f['status']}] {f['check']}")
        print(f"     {f['why']}")
        for e in f["evidence"][:8]:
            print(f"     - {e}")
        for h in f["hints"][:4]:
            print(f"     hint: {h}")
    print(NL + "  These establish what sdkconfig and partitions.csv say. Whether "
          "the resulting")
    print("  image behaves correctly is a different question, and no "
          "configuration file answers it.")
    return 0


# ========================================================== the compile loop
#
# SECTION3 is the first phase where the agent writes the artifact under
# governance rather than describing it, and where an oracle exists that the
# agent does not control. The compiler settles what no amount of reading can.
#
# Two hooks, and the quiet one carries the load:
#
#   PostToolUse   after a write to a build input, state the consequence: the
#                 archived log no longer describes this tree. Fails open, never
#                 blocks, no loop is possible.
#   Stop          blocks the turn ONLY when the model asserted build state as
#                 fact while the evidence says that state is unknown.
#
# The Stop hook is the only fail-CLOSED mechanism in this framework, and the
# documented Stop contract carries no loop protection and no stop_hook_active
# field. So the limits below are not caution, they are load-bearing:
#
#   - never block twice in a row
#   - never block more than STOP_MAX_BLOCKS times in a session
#   - never block when .no-stage-governance exists, or STAGE_KERNEL_NO_STOP is set
#   - fail open on any error, exactly like every other hook here

STOP_MAX_BLOCKS = 3
STOP_STATE_DIR = "stage-kernel-stop"

# Asserting a build or test result as fact. Deliberately narrow: a false
# positive here holds the engineer's turn hostage, which is the fastest way to
# get the whole mechanism switched off.
RE_BUILD_CLAIM = re.compile(
    r"\b(?:compiles|builds)\s+(?:cleanly|fine|successfully|without\s+\w+)"
    r"|\b(?:the\s+)?build\s+(?:succeeds|succeeded|passes|passed|is\s+clean|"
    r"went\s+through)"
    r"|\b(?:zero|no|0)\s+(?:compiler\s+)?warnings\b"
    r"|\b(?:it|this|that|the\s+code|the\s+firmware)\s+(?:now\s+)?compiles\b"
    r"|\ball\s+tests?\s+pass(?:ed|es)?\b"
    r"|\btests?\s+(?:pass|passed|are\s+green)\b",
    re.I)

# Any of these in the same sentence and the claim is not an assertion of fact:
# a plan, a conditional, a report of failure, or an explicit admission that it
# has not been checked.
RE_CLAIM_HEDGE = re.compile(
    r"\b(?:should|would|will|shall|expect\w*|assum\w+|presum\w+|probabl\w+|"
    r"likel\w+|may|might|ought|hopefully|in\s+theory|once|after|before|when|if|"
    r"unless|until|then|see|to\s+(?:check|confirm|verify|see))\b"
    # `error` is deliberately NOT here. "compiles without errors" is an
    # assertion, and hedging on the bare word suppressed it. The claim pattern
    # is narrow enough that a sentence reporting a failure does not match it.
    r"|\b(?:not|never|n't|cannot|can't|fails?|failed|failing)\b"
    r"|\b(?:unverified|unverifiable|unchecked|untested|not\s+yet)\b"
    r"|\b(?:run|rebuild|please\s+build|needs?\s+a\s+build)\b"
    # Citing evidence is not asserting a fresh fact. "The archived log shows
    # zero warnings, but it is stale" is exactly the honest sentence this
    # mechanism exists to encourage, and it fired before these were added.
    r"|\b(?:stale|archived|outdated|out\s+of\s+date|no\s+longer|previously|"
    r"according\s+to|reports?|reported|shows|showed|says|said|last\s+build)\b"
    r"|\?",
    re.I)

# A period inside a path is not a sentence end. Splitting on every dot cut
# "According to tests/reports/build-esp32s3.log there are zero warnings." in
# two and left the second half looking like a bare assertion.
RE_SENTENCE = re.compile(r"[^\n]+?(?:[.!?](?=\s|$)|\n|$)")


def _stop_state_path(session_id):
    d = Path(tempfile.gettempdir()) / STOP_STATE_DIR
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "nosession"))[:80]
    return d / f"{safe}.json"


def _stop_state(session_id):
    p = _stop_state_path(session_id)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return {"blocks": 0, "last_turn_blocked": False}


def _save_stop_state(session_id, state):
    p = _stop_state_path(session_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _unbound_targets(root: Path, state):
    """[(target, reason)] whose build evidence does not establish anything."""
    out = []
    for t in _gate_context(root, state).get("targets") or []:
        if not t.get("configured"):
            continue
        lb = t.get("last_build") or {}
        if lb.get("warnings") is None:
            out.append((t["target"], lb.get("log_binding")
                        or "no archived build log for this target"))
    return out


def _claim_sentences(message):
    """Sentences asserting a build or test result with no hedge in them."""
    hits = []
    for s in RE_SENTENCE.findall(message or ""):
        s = s.strip()
        if not s or not RE_BUILD_CLAIM.search(s):
            continue
        if RE_CLAIM_HEDGE.search(s):
            continue
        hits.append(" ".join(s.split())[:160])
    return hits


def cmd_post_tool_use(root: Path) -> int:
    """After a write, say what it did to the build evidence. Never blocks."""
    raw = (sys.stdin.read() or "").lstrip("\ufeff").strip()
    if not raw:
        return 0
    try:
        ev = json.loads(raw)
    except ValueError:
        return 0
    ti = ev.get("tool_input") or {}
    path = (ti.get("file_path") or ti.get("path") or "")
    if not path:
        return 0
    cwd = ev.get("cwd")
    if cwd and Path(cwd).is_dir():
        root = Path(cwd).resolve()
    ok, _ = detect_idf_project(root)
    if not ok or (root / SILENCE_NAME).exists():
        return 0
    # Only files the build actually consumes. A doc or a test source changes
    # nothing about whether the firmware still compiles.
    p = path.replace("\\", "/")
    name = p.split("/")[-1]
    if not (name in BUILD_INPUT_NAMES or name.startswith("sdkconfig")
            or any(p.lower().endswith(e) for e in
                   (".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".s"))):
        return 0
    if guards.NONFIRMWARE_SEGMENTS.intersection(
            s.lower() for s in p.split("/")[:-1]):
        return 0
    try:
        state, _ = load_state(root)
    except StateError:
        return 0
    try:
        stale = _unbound_targets(root, state)
    except Exception:                                  # noqa: BLE001
        return 0
    if not stale:
        return 0
    lines = [f"[stage-kernel] {name} is a build input. The archived build log no "
             f"longer establishes that this tree compiles:"]
    for tgt, why in stale[:3]:
        lines.append(f"  {tgt}: {str(why)[:220]}")
    lines.append("The Gate 2->3 zero-warning criterion reads UNVERIFIABLE until a "
                 "build is archived. Stating that it compiles is not something "
                 "this file can show.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": NL.join(lines)}}))
    return 0


def cmd_stop(root: Path) -> int:
    """Block the turn when a build claim outruns the evidence for it.

    The one fail-closed mechanism here. Everything about it is arranged so that
    a wrong decision costs one message rather than a spin.
    """
    raw = (sys.stdin.read() or "").lstrip("\ufeff").strip()
    if not raw:
        return 0
    try:
        ev = json.loads(raw)
    except ValueError:
        return 0
    if os.environ.get("STAGE_KERNEL_NO_STOP"):
        return 0
    cwd = ev.get("cwd")
    if cwd and Path(cwd).is_dir():
        root = Path(cwd).resolve()
    ok, _ = detect_idf_project(root)
    if not ok or (root / SILENCE_NAME).exists():
        return 0

    session = ev.get("session_id")
    st = _stop_state(session)
    message = ev.get("last_assistant_message") or ""

    claims = _claim_sentences(message)
    if not claims:
        if st.get("last_turn_blocked"):
            st["last_turn_blocked"] = False
            _save_stop_state(session, st)
        return 0

    try:
        state, _ = load_state(root)
        stale = _unbound_targets(root, state)
    except Exception:                                  # noqa: BLE001
        return 0                                       # fail open
    if not stale:
        st["last_turn_blocked"] = False
        _save_stop_state(session, st)
        return 0

    # Two limits, and they exist because the documented Stop contract has no
    # loop protection of its own. Blocking a turn the model cannot satisfy is
    # worse than letting one unevidenced sentence through.
    if st.get("last_turn_blocked"):
        print(f"[stage-kernel] the previous turn was already held for this. Not "
              f"holding again - the build claim stands unverified and it is "
              f"yours to judge.", file=sys.stderr)
        st["last_turn_blocked"] = False
        _save_stop_state(session, st)
        return 0
    if st.get("blocks", 0) >= STOP_MAX_BLOCKS:
        print(f"[stage-kernel] {STOP_MAX_BLOCKS} holds already in this session; "
              f"not holding again.", file=sys.stderr)
        return 0

    st["blocks"] = st.get("blocks", 0) + 1
    st["last_turn_blocked"] = True
    _save_stop_state(session, st)

    out = ["This turn states a build or test result as fact, and the archived "
           "evidence does not establish it:"]
    for c in claims[:2]:
        out.append(f'  claimed: "{c}"')
    for tgt, why in stale[:2]:
        out.append(f"  {tgt}: {str(why)[:240]}")
    out.append("")
    out.append("Run the build and let it answer - tools/idf_run.ps1 -Target "
               "<target> build archives a log, or use the esp-idf MCP "
               "build_project tool. If you would rather not, say plainly that "
               "the build state is unverified and stop.")
    out.append(f"(hold {st['blocks']} of {STOP_MAX_BLOCKS} this session; "
               f"STAGE_KERNEL_NO_STOP=1 disables this entirely)")
    print(NL.join(out), file=sys.stderr)
    return 2


def cmd_core(root: Path) -> int:
    """SECTION3 sec.3.1/4.1/4.3 - the agnostic core: pure, and measured."""
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(f"cannot check the core: {e}", file=sys.stderr)
        return 2
    bds = _build_dirs(root)
    findings = core_seam.run(root, state, bds[0] if bds else None)
    su = core_seam.summarise(findings)
    stage = (state.get("current") or {}).get("stage")
    print(f"AGNOSTIC CORE   stage {stage}")
    print(f"  machine-checked {su['machine_checked']} | refuted "
          f"{su['machine_refuted']} | unverifiable {su['unverifiable']}")
    for f in findings:
        print(NL + f"  [{f['status']}] {f['check']}")
        print(f"     {f['why']}")
        for e in f["evidence"][:8]:
            print(f"     - {e}")
        for h in f["hints"][:3]:
            print(f"     hint: {h}")
    print(NL + "  The seam is what makes the host build possible, and the host "
          "build is what")
    print("  sec.4.3 measures. Neither establishes that the core logic is right.")
    return 0


def cmd_design_review(root: Path) -> int:
    """SECTION2 sec.8 design review - an intra-stage review moment.

    Not a SECTION1 gate: it runs inside a stage, and its FAIL edge returns to
    the Measurable Requirements Table rather than to the artifact that failed.
    """
    try:
        state, _ = load_state(root)
    except StateError as e:
        print(f"cannot run the design review: {e}", file=sys.stderr)
        return 2
    d = spec_dir()
    if not d:
        print("SECTION2 not reachable - set EMBEDDED_WORKFLOW_SPEC_DIR",
              file=sys.stderr)
        return 2
    criteria = gates.parse_design_review(d)
    if not criteria:
        print("SECTION2 sec.8 checklist not found", file=sys.stderr)
        return 2

    ctx = _gate_context(root, state)
    ctx["spec_dir"] = str(d)
    rows, orphans, universal = gates.evaluate_design_review(
        criteria, ctx, (state.get("attestations") or []))
    summary = gates.summarise(rows)
    rec, why = gates.recommendation(summary)

    print(f"DESIGN REVIEW (SECTION2 sec.8)   RECOMMENDATION: {rec}  ({why})")
    print(f"  total {summary['total']} | machine-checked "
          f"{summary['machine_checked']} | refuted {summary['machine_refuted']} "
          f"| attested {summary['human_attested']} | unverifiable "
          f"{summary['unverifiable']}")
    print(f"  {universal} of {summary['total']} criteria make a universal claim "
          f"(every / all / each) over a set no file enumerates. That count is "
          f"the review's most useful output, and it needs no checks at all.")
    if orphans:
        print(f"  ! checks whose anchor matched nothing: {', '.join(orphans)}")

    for i, r in enumerate(rows, 1):
        if r["status"] == gates.UNVERIFIABLE:
            continue
        print(NL + f"  {i}. [{r['status']}] {r['criterion'][:100]}")
        print(f"     {r['why']}")
        for e in r["evidence"][:4]:
            print(f"     evidence: {e}")

    unver = [r for r in rows if r["status"] == gates.UNVERIFIABLE]
    if unver:
        print(NL + f"  UNVERIFIABLE ({len(unver)}) - neither machine-checked nor "
              f"attested:")
        for r in unver[:12]:
            print(f"    - {r['criterion'][:96]}")
        if len(unver) > 12:
            print(f"    ... and {len(unver) - 12} more")

    print(NL + "  This is a recommendation, not a decision. Record the outcome "
          "as a design_review_decided event in stage-state.yaml.")
    return 0


# ================================================================== self-test
#
# GUARD_SPEC.md carried `numeric-claim | strict` for as long as the registry
# carried `guard`. Nothing could notice, because a markdown table is a fact
# stored by hand - precisely what invariant I2 forbids the engineer from doing,
# applied here to the framework's own documentation.
#
# Correcting the cell fixes one instance. This makes the class detectable.

_SPEC_ROW = re.compile(r"\s*\|\s*`([a-z][a-z0-9-]*)`\s*\|\s*([a-z]+)\s*\|(.*?)\|\s*(.)")


def _spec_guard_table():
    """[(id, level, implemented)] as hooks/GUARD_SPEC.md claims them."""
    f = Path(__file__).resolve().parent.parent / "hooks" / "GUARD_SPEC.md"
    if not f.is_file():
        return None
    rows = [(m.group(1), m.group(2), m.group(4) == "\u2705")
            for line in f.read_text(encoding="utf-8").splitlines()
            for m in [_SPEC_ROW.match(line)] if m]
    return rows or None


def _check_legacy_headers():
    """Every replacement this framework recommends must exist in the installed IDF.

    `driver/mcpwm.h -> driver/mcpwm_prelude` shipped for weeks: the header name
    was missing its extension, so the guard fired correctly and then handed the
    engineer a path that does not exist. The table was written from the migration
    notes and never checked against the tree it describes.
    """
    idf = os.environ.get("IDF_PATH")
    if not idf or not Path(idf).is_dir():
        return ["legacy-header table NOT checked: IDF_PATH is unset, so the "
                "replacements this framework recommends were not confirmed to "
                "exist (set IDF_PATH and re-run)"], True
    have = set()
    for p in Path(idf).rglob("include/**/*.h"):
        s = p.as_posix()
        i = s.rfind("/include/")
        if i >= 0:
            have.add(s[i + 9:])
    if not have:
        return ["legacy-header table NOT checked: no headers found under "
                f"{idf}"], True
    bad = []
    for hdr, repl in guards.LEGACY_HEADERS.items():
        cands = re.findall(r"[a-z0-9_]+/[a-z0-9_]+\.h", repl)
        if not cands:
            bad.append(f"`{hdr}` -> the replacement text names no header file")
            continue
        for c in cands:
            if c not in have:
                bad.append(f"`{hdr}` -> recommends `{c}`, which does not exist "
                           f"in the installed IDF")
    return bad, False

def _check_extracted_freshness():
    """Extracted copies must still match the specification they were cut from.

    rsmr-matrix.yaml and quality-attributes.yaml are derived files. A derived
    file whose source has moved on is the stale-able fact invariant I2 exists to
    catch, so each records the sha256 it was cut from.
    """
    here = Path(__file__).resolve().parent.parent
    bad = []
    checked = 0
    # kconfig-migration.yaml is cut from the installed ESP-IDF rather than from a
    # workflow document, so it is stamped with the IDF version instead of a file
    # hash. An upgrade silently invalidates every symbol verdict built on it.
    kf = here / "kconfig-migration.yaml"
    if kf.is_file():
        try:
            import yaml
            kd = yaml.safe_load(kf.read_text(encoding="utf-8")) or {}
            recorded = str(kd.get("idf_version") or "")
            live = None
            idfp = os.environ.get("IDF_PATH")
            if idfp:
                vh = (Path(idfp) / "components" / "esp_common" / "include"
                      / "esp_idf_version.h")
                if vh.is_file():
                    t = vh.read_text(encoding="utf-8", errors="ignore")
                    got = [re.search(rf"#define\s+ESP_IDF_VERSION_{k}\s+(\d+)", t)
                           for k in ("MAJOR", "MINOR", "PATCH")]
                    if all(got):
                        live = ".".join(m.group(1) for m in got)
            if live and recorded and live != recorded:
                bad.append(f"kconfig-migration.yaml was cut from ESP-IDF "
                           f"v{recorded}, the installed tree is v{live} - "
                           f"regenerate with tools/extract_kconfig.py")
            elif live:
                checked += 1
        except Exception:                              # noqa: BLE001
            bad.append("kconfig-migration.yaml did not parse")
    else:
        bad.append("kconfig-migration.yaml is absent - run "
                   "tools/extract_kconfig.py")

    for name, fields in (("rsmr-matrix.yaml",
                          [("source", "source_sha256"),
                           ("debt_source", "debt_source_sha256")]),
                         ("quality-attributes.yaml",
                          [("source", "source_sha256"),
                           ("criteria_source", "criteria_source_sha256")])):
        f = here / name
        if not f.is_file():
            bad.append(f"{name} is absent - regenerate it")
            continue
        try:
            import yaml
            d = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001
            bad.append(f"{name} did not parse: {exc}")
            continue
        for pkey, skey in fields:
            src, rec = d.get(pkey), d.get(skey)
            if not src:
                continue
            for base in (here.parent, Path.cwd()):
                cand = base / src
                if cand.is_file():
                    checked += 1
                    if str(rec) != sha256_file(cand):
                        bad.append(f"{name}: {src} changed since extraction "
                                   f"({short(str(rec))} -> "
                                   f"{short(sha256_file(cand))}) - regenerate")
                    break
            else:
                bad.append(f"{name}: {src} not reachable from here")
    return bad, checked

_SCHEMA_ROW = re.compile(r"^\|\s*`([a-z_]+)`(?:\s*/\s*`([a-z_]+)`)?\s*\|"
                         r"\s*([^|]*?)\s*\|")
_SCHEMA_FIELD = re.compile(r"`([a-z_]+)(?:\[\])?`")


def _schema_log_fields():
    """{event: [fields]} as STAGE_STATE_SCHEMA.md sec.7 tabulates them."""
    f = Path(__file__).resolve().parent.parent / "STAGE_STATE_SCHEMA.md"
    if not f.is_file():
        return None
    out = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        m = _SCHEMA_ROW.match(line)
        if not m:
            continue
        names = [n for n in (m.group(1), m.group(2)) if n]
        fields = _SCHEMA_FIELD.findall(m.group(3))
        if not fields:
            continue
        for n in names:
            if n in LOG_EVENT_FIELDS or n in out:
                out[n] = fields
    return out or None


def _check_log_field_table():
    """Drift between the enforced table and the documented one."""
    doc = _schema_log_fields()
    if doc is None:
        return ["STAGE_STATE_SCHEMA.md: no log-event table found to compare "
                "against"], 0
    bad = []
    for ev in sorted(set(LOG_EVENT_FIELDS) | set(doc)):
        code, d = LOG_EVENT_FIELDS.get(ev), doc.get(ev)
        if code is None:
            bad.append(f"STAGE_STATE_SCHEMA.md documents event `{ev}`, which "
                       f"LOG_EVENT_FIELDS does not enforce")
        elif d is None:
            bad.append(f"LOG_EVENT_FIELDS enforces `{ev}`, which "
                       f"STAGE_STATE_SCHEMA.md sec.7 does not document")
        elif sorted(code) != sorted(d):
            only_c = sorted(set(code) - set(d))
            only_d = sorted(set(d) - set(code))
            parts = []
            if only_c:
                parts.append("enforced but undocumented: " + ", ".join(only_c))
            if only_d:
                parts.append("documented but unenforced: " + ", ".join(only_d))
            bad.append(f"`{ev}` field list differs - " + "; ".join(parts))
    return bad, len(LOG_EVENT_FIELDS)


def cmd_selftest(root: Path) -> int:
    """Check the framework's documentation against the framework's code."""
    bad = []
    spec = _spec_guard_table()
    if spec is None:
        bad.append("hooks/GUARD_SPEC.md: no guard table found to compare against")
    else:
        code = {g[0]: (g[1], bool(g[4])) for g in guards.REGISTRY}
        doc = {r[0]: (r[1], r[2]) for r in spec}
        for gid in sorted(set(code) | set(doc)):
            if gid not in code:
                bad.append(f"GUARD_SPEC.md documents `{gid}`, which the registry "
                           f"does not define")
            elif gid not in doc:
                bad.append(f"the registry defines `{gid}`, which GUARD_SPEC.md "
                           f"does not document")
            else:
                if code[gid][0] != doc[gid][0]:
                    bad.append(f"`{gid}`: registry level is {code[gid][0]}, "
                               f"GUARD_SPEC.md says {doc[gid][0]}")
                if code[gid][1] != doc[gid][1]:
                    bad.append(f"`{gid}`: registry implemented={code[gid][1]}, "
                               f"GUARD_SPEC.md says {doc[gid][1]}")
        missing = [g for g in guards.PRECONDITIONS if g not in doc]
        if missing:
            bad.append(f"guards with a precondition but no GUARD_SPEC row: "
                       f"{', '.join(sorted(missing))}")

    hdr_bad, hdr_skipped = _check_legacy_headers()
    fresh_bad, fresh_n = _check_extracted_freshness()
    bad += fresh_bad
    lf_bad, lf_n = _check_log_field_table()
    bad += lf_bad

    print("FRAMEWORK SELF-TEST")
    if hdr_skipped:
        for h in hdr_bad:
            print(f"  ~ {h}")
        hdr_bad = []
    bad += hdr_bad
    if bad:
        print(f"  {len(bad)} drift(s) between documentation and code:")
        for b in bad:
            print(f"    - {b}")
        print(NL + "  A hand-written doc is a stale-able fact (invariant I2). "
              "Fix the doc or the code, then re-run.")
        return 1
    print(f"  guard registry vs hooks/GUARD_SPEC.md: {len(guards.REGISTRY)} guards "
          f"agree on id, level, and implementation status")
    if not hdr_skipped:
        print(f"  legacy-header table vs installed IDF: all "
              f"{len(guards.LEGACY_HEADERS)} recommended replacements exist")
    print(f"  extracted copies vs their specification: {fresh_n} source hash(es) "
          f"still match")
    print(f"  log-event fields vs STAGE_STATE_SCHEMA.md sec.7: {lf_n} event(s) "
          f"agree")
    return 0


def main() -> int:
    # The digest is piped into an agent context across shells whose default
    # code page is not UTF-8. Mojibake in injected context is worse than no
    # context, so force the encoding rather than trusting the console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="ESP32 Stage Kernel - layer 1")
    ap.add_argument("command",
                choices=["detect", "cache", "check", "digest", "guard", "gate",
                         "design", "spec-stamp", "design-review",
                         "rsmr", "rsmr-scorecard", "config",
                         "core", "post-tool-use", "stop", "selftest"])
    ap.add_argument("-C", "--directory", default=".", help="project root")
    a = ap.parse_args()
    root = Path(a.directory).resolve()
    return {"detect": cmd_detect, "cache": cmd_cache, "check": cmd_check,
            "digest": cmd_digest, "guard": cmd_guard,
            "gate": cmd_gate, "design": cmd_design,
            "spec-stamp": cmd_spec_stamp,
            "design-review": cmd_design_review,
            "rsmr": cmd_rsmr, "rsmr-scorecard": cmd_rsmr_scorecard,
            "config": cmd_config,
            "core": cmd_core,
            "post-tool-use": cmd_post_tool_use, "stop": cmd_stop,
            "selftest": cmd_selftest}[a.command](root)


if __name__ == "__main__":
    sys.exit(main())
