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
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import guards
    import gates
    import design_check
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


def check_consistency(state, folded, root: Path = Path(".")) -> list[str]:
    """SCHEMA §10. Returns a list of human-readable failures."""
    f = []
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


def _build_log_evidence(root: Path, target: str):
    """Warning count from an archived build log, with provenance.

    The digest reported build_warnings as unknown from the day it was written,
    because nothing captured a build log. This reads one if it exists: evidence
    is a file on disk with a path and a timestamp, never a number the kernel
    inferred. No log means the value stays unknown - it never becomes zero.
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
    except OSError:
        return None
    warn = len(re.findall(r"^.*?: warning: ", text, re.M))
    err = len(re.findall(r"^.*?: error: ", text, re.M))
    return {
        "warnings": warn,
        "errors": err,
        "source": log.relative_to(root).as_posix(),
        "at": datetime.fromtimestamp(log.stat().st_mtime)
              .astimezone().isoformat(timespec="seconds"),
    }


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
        cfgp = Path(data.get("config_file") or (root / "sdkconfig"))
        if not cfgp.is_absolute():
            cfgp = (root / cfgp).resolve()
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
        ev = _build_log_evidence(root, tgt)
        if ev:
            lb = entry.setdefault("last_build", {"artifact": None, "at": None})
            lb["warnings"] = ev["warnings"]
            lb["errors"] = ev["errors"]
            lb["log"] = ev["source"]
            lb["log_at"] = ev["at"]
        found[tgt] = entry

    root_sdk = root / "sdkconfig"
    if root_sdk.is_file():
        cfg = parse_sdkconfig(root_sdk)
        tgt = cfg.get("CONFIG_IDF_TARGET")
        if tgt and tgt not in found:
            found[tgt] = _target_from_cfg(cfg, root_sdk, tgt, "sdkconfig")

    for t in (intent or []):
        if t not in found:
            found[t] = {"target": t, "configured": False,
                        "note": "declared in intent but no build dir or sdkconfig found"}
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


def spec_defect_status(reg):
    """Returns (stale_reason, defects) - stale_reason is None when trustworthy.

    A curated fact list is exactly what invariant I2 warns against, so the
    register carries the hash of the specification it was verified against. A
    changed specification makes it detectable rather than silently trusted."""
    if not reg:
        return "register absent", []
    src = reg.get("source")
    stale = None
    if src:
        for base in (Path(__file__).resolve().parents[2], Path.cwd()):
            cand = base / src
            if cand.is_file():
                actual = sha256_file(cand)
                # Coerce: an all-digit hash written unquoted is parsed by YAML
                # as an integer, and an integer 0 is falsy - which would report
                # "not stamped" for a hash that is merely wrong. Found by test.
                recorded = reg.get("spec_sha256")
                recorded = str(recorded) if recorded is not None else None
                if not recorded or recorded == "None":
                    stale = ("baseline not stamped - run "
                             "'stage_kernel.py spec-stamp'")
                elif recorded != actual:
                    stale = (f"{src} changed since verification "
                             f"({short(recorded)} -> {short(actual)}) - re-verify")
                break
        else:
            stale = f"{src} not reachable from here"
    return stale, (reg.get("defects") or [])


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
        if lb and lb.get("warnings"):
            L.append(f"      # WARNING {t['target']}: {lb['warnings']} compiler "
                     f"warning(s) in {lb.get('log')} - Gate 2->3 requires zero")
        if t.get("warn_suppress_on"):
            L.append(f"      # WARNING {t['target']}: warning suppression enabled "
                     f"({', '.join(t['warn_suppress_on'])}) — this is a gate finding")
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
         f"  active_guards: "
         f"{ylist(guards.implemented_guards(cur.get('enforcement')))}",
         "  guards_denying: " + ("false   # advisory: findings are surfaced as "
                                 "context, never denied"
                                 if cur.get('enforcement') == 'advisory' else "true"),
         "",
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
    return {
        "targets": targets,
        "kconfig_symbols": syms or None,
        "idf_version": (gt.get("idf_installed") or {}).get("version"),
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
    return {"root": root, "state": state,
            "targets": gt.get("targets") or [],
            "idf_version": (gt.get("idf_installed") or {}).get("version"),
            "kconfig_symbols": None}


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
    src = reg.get("source")
    spec = None
    for base in (Path(__file__).resolve().parents[2], Path.cwd()):
        if (base / src).is_file():
            spec = base / src
            break
    if spec is None:
        print(f"{src} not reachable", file=sys.stderr)
        return 2
    digest_hex = sha256_file(spec)
    text = reg_path.read_text(encoding="utf-8")
    text = re.sub(r"^spec_sha256:.*$", f"spec_sha256: {digest_hex}",
                  text, count=1, flags=re.M)
    reg_path.write_text(text, encoding="utf-8")
    print(f"stamped {SPEC_DEFECTS_FILE} against {src} ({short(digest_hex)})")
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
                         "design", "spec-stamp", "design-review"])
    ap.add_argument("-C", "--directory", default=".", help="project root")
    a = ap.parse_args()
    root = Path(a.directory).resolve()
    return {"detect": cmd_detect, "cache": cmd_cache, "check": cmd_check,
            "digest": cmd_digest, "guard": cmd_guard,
            "gate": cmd_gate, "design": cmd_design,
            "spec-stamp": cmd_spec_stamp,
            "design-review": cmd_design_review}[a.command](root)


if __name__ == "__main__":
    sys.exit(main())
