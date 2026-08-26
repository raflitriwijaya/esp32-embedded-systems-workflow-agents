#!/usr/bin/env python3
"""ESP32 Stage Kernel - layer 3 guards.

Design principle, and the reason this list is short:

    Do not guard what the compiler already reports. A guard earns its cost only
    where the toolchain is silent.

Applied, that ranks the guards by the damage a silent failure does, not by how
easy the pattern is to match. `header-exists` was designed and then dropped: a
missing header is a loud, unambiguous compile error, so a guard adds nothing but
false positives.

Each check returns findings; severity is decided by the caller from the
project's `enforcement` level (see decide()). At `advisory` nothing is ever
denied - findings are injected as context instead, so a Stage 1 project gets the
warning without the friction.
"""

from __future__ import annotations

import re

SOURCE_EXT = (".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".ino")
# Matched against PATH SEGMENTS, not substrings: the hook receives relative
# paths as often as absolute ones, and a "/build/" substring test silently
# fails to skip "build/x/gen.c". Generated and vendored code tripping these
# guards on every write is the fastest way to get the whole layer switched off.
SKIP_SEGMENTS = {"build", "managed_components", ".git", "third_party",
                 "node_modules", "dist"}
SKIP_PREFIXES = ("build_", "espressif__", "cmake-build")

# ESP-IDF v6.0 removed these outright. driver/i2c.h is the exception that makes
# this guard worth having: it is EOL, not removed, so it still compiles.
LEGACY_HEADERS = {
    "driver/adc.h": "esp_adc/adc_oneshot.h or esp_adc/adc_continuous.h (removed in v6.0)",
    "driver/dac.h": "driver/dac_oneshot.h or driver/dac_continuous.h (removed in v6.0)",
    "driver/i2s.h": "driver/i2s_std.h or driver/i2s_pdm.h (removed in v6.0)",
    "driver/timer.h": "driver/gptimer.h (removed in v6.0)",
    "driver/pcnt.h": "driver/pulse_cnt.h (removed in v6.0)",
    "driver/mcpwm.h": "driver/mcpwm_prelude (removed in v6.0)",
    "driver/rmt.h": "driver/rmt_tx.h and driver/rmt_rx.h (removed in v6.0)",
    "driver/sigmadelta.h": "driver/sdm.h (removed in v6.0)",
    "driver/temp_sensor.h": "driver/temperature_sensor.h (removed in v6.0)",
    "driver/i2c.h": "driver/i2c_master.h or driver/i2c_slave.h "
                    "(EOL in v6.0, removal scheduled for v7.0 - still compiles today, "
                    "which is exactly why this is worth catching now)",
}
LEGACY_CALLS = {
    "adc1_get_raw": "adc_oneshot_read()",
    "adc2_get_raw": "adc_oneshot_read()",
    "i2c_cmd_link_create": "i2c_master_transmit() / i2c_master_receive()",
    "i2c_driver_install": "i2c_new_master_bus() + i2c_master_bus_add_device()",
    "timer_group_set_alarm_value_in_isr": "gptimer alarm callbacks",
}
ARDUINO_MARKERS = ("Arduino.h", "pinMode(", "digitalWrite(", "digitalRead(",
                   "analogWrite(")
WARN_SUPPRESS = ("CONFIG_COMPILER_DISABLE_DEFAULT_ERRORS",
                 "CONFIG_COMPILER_DISABLE_GCC15_WARNINGS")


def _f(guard, msg, cite, line=None):
    return {"guard": guard, "message": msg, "cite": cite, "line": line}


def _lineno(text, idx):
    return text.count("\n", 0, idx) + 1


def _strip_line_comments(text):
    """Blank out // and /* */ so code scans do not trip over prose."""
    text = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), text, flags=re.S)
    return re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), text)


# ============================================================ checks

def g_stack_unit(path, text, ctx):
    """ESP-IDF takes stack depth in BYTES. Vanilla FreeRTOS takes words.

    This is the highest-value guard in the set: the mistake compiles cleanly and
    under-allocates by 4x. This project shipped it in three of its own
    specification documents before it was caught.
    """
    out = []
    code = _strip_line_comments(text)
    if re.search(r"\bxTaskCreate", code):
        for m in re.finditer(r"sizeof\s*\(\s*StackType_t\s*\)", code):
            out.append(_f("stack-unit",
                          "sizeof(StackType_t) is a vanilla-FreeRTOS word-count idiom. "
                          "ESP-IDF xTaskCreate/xTaskCreatePinnedToCore take stack depth "
                          "in BYTES - multiplying by the word size over-allocates 4x, "
                          "and dividing under-allocates 4x.",
                          "SECTION5 RL-ESP-06; SECTION2 sec.7.1",
                          _lineno(code, m.start())))
    for m in re.finditer(
            r"^[^\n]*#define\s+\w*STACK\w*\s+[^\n/]*(?://|/\*)[^\n]*\bwords?\b",
            text, re.M | re.I):
        out.append(_f("stack-unit",
                      "a stack-size definition is annotated 'words'. On ESP-IDF the "
                      "unit is BYTES; SECTION5 RL-ESP-06 requires the comment to say "
                      "'bytes' explicitly.",
                      "SECTION5 RL-ESP-06",
                      _lineno(text, m.start())))
    for m in re.finditer(r"xTaskCreate\w*\s*\([^;]{0,400}?\bwords?\b", text, re.I | re.S):
        if "sizeof(StackType_t)" not in m.group(0):
            out.append(_f("stack-unit",
                          "a task creation call is annotated 'words'. ESP-IDF stack "
                          "depth is in BYTES.",
                          "SECTION5 RL-ESP-06",
                          _lineno(text, m.start())))
    return out


def g_kconfig_exists(path, text, ctx):
    """A misspelled CONFIG_ symbol evaluates false and silently disables a feature.

    The canonical case is CONFIG_ESP32_TASK_WDT_TIMEOUT_S (a v4-era name) against
    CONFIG_ESP_TASK_WDT_TIMEOUT_S: the watchdog is quietly never configured.
    """
    known = ctx.get("kconfig_symbols")
    if not known:
        return []                       # cannot verify - say nothing rather than guess
    defined_here = set(re.findall(r"#\s*define\s+(CONFIG_[A-Z0-9_]+)", text))
    out, seen = [], set()
    for m in re.finditer(r"\b(CONFIG_[A-Z0-9_]{2,})\b", text):
        sym = m.group(1)
        if sym in known or sym in defined_here or sym in seen:
            continue
        seen.add(sym)
        out.append(_f("kconfig-exists",
                      f"{sym} is not present in any sdkconfig known to this project. "
                      f"A CONFIG_ symbol that does not exist evaluates false and "
                      f"disables the feature silently. Verify the spelling against "
                      f"menuconfig - v4/v5-era names were widely renamed in v5 and v6.",
                      "SECTION3 sec.2.2",
                      _lineno(text, m.start())))
    return out


def g_core_pin(path, text, ctx):
    """Pinning to a core the target does not have fails at runtime, not build time."""
    out = []
    code = _strip_line_comments(text)
    unicore = [t for t in ctx.get("targets", [])
               if t.get("configured") and t.get("unicore")]
    for m in re.finditer(r"xTaskCreatePinnedToCore\s*\((.*?)\)\s*;", code, re.S):
        args = m.group(1)
        depth, buf, parts = 0, "", []
        for ch in args:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(buf.strip()); buf = ""
            else:
                buf += ch
        parts.append(buf.strip())
        if len(parts) < 7:
            continue
        core = parts[-1]
        if not re.fullmatch(r"\d+", core):
            continue                    # tskNO_AFFINITY or a variable - not decidable
        n = int(core)
        ln = _lineno(code, m.start())
        if n >= 2:
            out.append(_f("core-pin",
                          f"core {n} does not exist on any current ESP32 target.",
                          "CLAUDE.md sec.2", ln))
        elif n >= 1 and unicore:
            names = ", ".join(t["target"] for t in unicore)
            out.append(_f("core-pin",
                          f"core {n} is pinned, but {names} is configured with "
                          f"CONFIG_FREERTOS_UNICORE=y and has only core 0. This builds "
                          f"cleanly and fails on the device. Use tskNO_AFFINITY, or "
                          f"guard the call per target.",
                          "CLAUDE.md sec.2; SECTION2 sec.7.1", ln))
    return out


def g_warn_suppress(path, text, ctx):
    """Turning warnings-as-errors off is the cheapest way to fake Gate 2->3."""
    out = []
    for sym in WARN_SUPPRESS:
        m = re.search(rf"^{sym}=y\s*$", text, re.M)
        if m:
            out.append(_f("warn-suppress",
                          f"{sym}=y disables the ESP-IDF v6.0 warnings-as-errors "
                          f"default. The Gate 2->3 criterion 'compiles with zero "
                          f"warnings' would then be satisfied by suppression rather "
                          f"than by fixing the code. Enabling it is a gate finding, "
                          f"not a build fix.",
                          "SECTION1 sec.3 Gate 2->3; SECTION3 sec.2.2",
                          _lineno(text, m.start())))
    return out


def g_legacy_driver(path, text, ctx):
    out = []
    ver = (ctx.get("idf_version") or "")
    pre6 = bool(re.match(r"[0-5]\.", ver))
    for hdr, repl in LEGACY_HEADERS.items():
        m = re.search(rf'#\s*include\s*[<"]{re.escape(hdr)}[>"]', text)
        if m:
            note = ("" if not pre6 else
                    f" (installed IDF is {ver}, where this is deprecated rather than "
                    f"removed - it will break on upgrade)")
            out.append(_f("legacy-driver",
                          f"{hdr} -> use {repl}{note}",
                          "SECTION3 sec.2.2 (v6.0 migration)",
                          _lineno(text, m.start())))
    code = _strip_line_comments(text)
    for fn, repl in LEGACY_CALLS.items():
        m = re.search(rf"\b{re.escape(fn)}\s*\(", code)
        if m:
            out.append(_f("legacy-driver",
                          f"{fn}() belongs to a driver removed or retired in "
                          f"ESP-IDF v6.0 -> use {repl}",
                          "SECTION3 sec.2.2 (v6.0 migration)",
                          _lineno(code, m.start())))
    return out


def g_arduino(path, text, ctx):
    """Redundant with the compiler, but SECTION3 sec.3.3 mandates the check."""
    out = []
    code = _strip_line_comments(text)
    for mark in ARDUINO_MARKERS:
        idx = code.find(mark)
        if idx >= 0:
            out.append(_f("arduino-ban",
                          f"'{mark}' is an Arduino-core construct. CLAUDE.md sec.2 fixes "
                          f"this platform to ESP-IDF only.",
                          "CLAUDE.md sec.2; SECTION3 sec.3.3",
                          _lineno(code, idx)))
    return out


# Units that mark a line as asserting a measurement rather than a design intent.
# Hole 3 (verified): the electrical and mechanical units carrying every hard
# number in SECTION2 sec.4.1/sec.4.2 - the hardware bring-up checklist - were
# absent, so "Supply rail measured at 3.28 V" passed silently.
_MEASURED_UNIT = (
    r"(?:bytes?|B|KB|kB|MiB|ms|us|µs|s|dBm|mA|uA|µA|A|mV|µV|V|"
    r"MHz|kHz|Hz|%|°C|degC|hours?|h|"
    r"kΩ|MΩ|Ω|kOhm|MOhm|Ohm|"
    r"pF|nF|µF|uF|F|mW|W|mm|cm|mil|ppm)")
# Anything that shows where a number came from.
# Hole 2 (verified): REQ- was treated as a citation. It is not. A requirement
# is what a number must SATISFY, not where it came from - "12 uA, satisfying
# REQ-S2-009" cites nothing. ASM- stays: an assumption is a legitimate trace,
# because it says openly that the number is not yet evidenced.
_CITATION = re.compile(
    r"(tests/reports/|\.log\b|\.json\b|\.csv\b|ASM-|TRACE-|TEST-|"
    r"datasheet|measured on|measurement record|per SECTION|@[0-9a-f]{4,}|"
    r"KERNEL_OBS)", re.I)
# Ranges, targets and budgets are intentions, not measurements.
# A CONTRACT BOUND is not a measurement. "Blocking: <= 10 ms" in a port
# interface is a promise the implementation must keep; flagging it would flood
# every correctly-written sec.5.4 header. Found by testing: CLOSURE_SPEC had
# ASSERTED this case was already exempt, and it was not.
#
# The distinguishing feature is a relational operator or a contract keyword -
# NOT the word "measured". "worst-case measured at 15.1 ms" carries no operator
# and must still fire, because that phrasing is exactly how a datasheet figure
# gets relabelled as an observation.
_INTENT = re.compile(r"(target|budget|shall|must|requirement|limit|threshold|"
                     r"at least|no more than|not exceed|maximum|minimum|range|"
                     r"blocking|reentrant|timeout|deadline|period|interval|"
                     r"TBD|e\.g\.|example|template|<[A-Za-z_ ]+>|"
                     r"<=|>=|≤|≥)", re.I)


# Hole 1 (verified): every line beginning with "|" was skipped, so the
# Measurable Target column of the SECTION2 sec.2.1 requirements table - the
# highest-risk surface in the whole chapter - was invisible.
#
# The fix is NOT to stop skipping tables. Design tables legitimately carry
# contract and target statements, and firing on those would flood a correct
# document. Instead: parse the table, and inspect only the columns that hold
# measurements. Column identity is what makes this decidable, and it is why
# this guard needs the register pointers from the context layer.
_MEASUREMENT_COLUMNS = ("measurable target", "measured", "measurement",
                        "actual", "observed", "result", "value")
_PROSE_COLUMNS = ("requirement", "description", "rationale", "content", "rule",
                  "note", "notes", "column", "criterion", "alternatives")
# SECTION2 sec.2.1: "If the measurable target relies on an unvalidated premise,
# the assumption ID must be logged." The Assumption column IS the table's
# provenance mechanism, so a citation may legitimately sit in a DIFFERENT cell
# of the same row. Checking cells in isolation flagged a correctly-filled row -
# found by testing against the specification's own worked example.
_TRACE_COLUMNS = ("assumption", "source", "evidence", "reference", "trace")


def _split_row(line):
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_separator(line):
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def _measurement_cells(lines, i):
    """If lines[i] starts a markdown table, yield (line_no, cell, trace) for
    cells under a measurement column. `trace` is the joined text of that row's
    trace columns, so a citation held elsewhere in the row still counts.

    Returns (entries, rows_consumed). A table whose header names no measurement
    column is skipped entirely - the conservative direction, and what keeps
    contract tables quiet."""
    if i + 1 >= len(lines) or not _is_separator(lines[i + 1]):
        return [], 0
    header = [h.lower() for h in _split_row(lines[i])]
    wanted = [n for n, h in enumerate(header)
              if any(k in h for k in _MEASUREMENT_COLUMNS)
              and not any(k in h for k in _PROSE_COLUMNS)]
    tracecols = [n for n, h in enumerate(header)
                 if any(k in h for k in _TRACE_COLUMNS)]
    j, out = i + 2, []
    while j < len(lines) and lines[j].strip().startswith("|"):
        cells = _split_row(lines[j])
        trace = " ".join(cells[n] for n in tracecols if n < len(cells))
        for n in wanted:
            if n < len(cells) and cells[n]:
                out.append((j + 1, cells[n], trace))
        j += 1
    return out, j - i


def g_numeric_claim(path, text, ctx):
    """A figure with a unit, stated as fact, with nothing showing where it came from.

    Strict level only. This is the textual counterpart of the closure loop: a
    measurement that never happened reads exactly like one that did, and the
    difference is a citation.
    """
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        st = line.strip()

        if st.startswith("|"):
            cells, consumed = _measurement_cells(lines, i)
            for ln, cell, trace in cells:
                if not re.search(rf"\b\d[\d.,]*\s*{_MEASURED_UNIT}\b", cell):
                    continue
                # Citation may sit in the cell OR in the row's trace columns.
                # Intent is judged on the cell alone: a "shall" in the adjacent
                # Requirement column must not exempt the measurement.
                if _CITATION.search(cell) or _CITATION.search(trace):
                    continue
                if _INTENT.search(cell):
                    continue
                out.append(_f("numeric-claim",
                              "a measurement column states a figure with no "
                              "citation. Give the measurement record or report "
                              "it came from, or name the assumption (ASM-) it "
                              "rests on. The Assumption column exists for this.",
                              "SECTION2 sec.2.1 Measurable Target; "
                              "SECTION1 sec.5 evidence tiers", ln))
            i += consumed if consumed else 1
            continue

        if not st or st.startswith(("#", ">", "-", "*", "`")):
            i += 1
            continue
        if not re.search(rf"\b\d[\d.,]*\s*{_MEASURED_UNIT}\b", st):
            i += 1
            continue
        if _CITATION.search(st) or _INTENT.search(st):
            i += 1
            continue
        out.append(_f("numeric-claim",
                      "a figure with a unit is stated as fact with no citation. "
                      "Cite the log, report, or datasheet it came from, or mark "
                      "it as an assumption (ASM-) until it is measured.",
                      "SECTION1 sec.5 evidence tiers; SECTION5 RL-ESP-06",
                      i + 1))
        i += 1
    return out[:8]


# ============================================================ registry

def _is_source(path):
    p = path.replace("\\", "/")
    segs = p.split("/")
    if SKIP_SEGMENTS.intersection(segs):
        return False
    if any(seg.startswith(SKIP_PREFIXES) for seg in segs):
        return False
    return p.lower().endswith(SOURCE_EXT)


def _is_claim_doc(path):
    """Documents that feed gate criteria. Deliberately excludes tests/reports/,
    which holds the evidence itself, and this framework's own specs."""
    p = path.replace(chr(92), "/")
    segs = p.split("/")
    if SKIP_SEGMENTS.intersection(segs) or "reports" in segs:
        return False
    if not p.lower().endswith(".md"):
        return False
    return any(seg in segs for seg in ("design", "hardware", "reliability",
                                      "pic-audit"))


def _is_sdkconfig(path):
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.startswith("sdkconfig")


REGISTRY = [
    # id,             level,      applies,       fn,               implemented
    ("stack-unit",    "guard",    _is_source,    g_stack_unit,     True),
    ("kconfig-exists", "guard",   _is_source,    g_kconfig_exists, True),
    ("core-pin",      "guard",    _is_source,    g_core_pin,       True),
    ("warn-suppress", "guard",    _is_sdkconfig, g_warn_suppress,  True),
    ("legacy-driver", "guard",    _is_source,    g_legacy_driver,  True),
    ("arduino-ban",   "guard",    _is_source,    g_arduino,        True),
    # Hole 4 (verified): at "strict" this guard denies only at S4-S5, while
    # the design phase runs at S1-S3 - so it could never deny during the
    # work it exists to protect. At "guard" it warns at S1 and denies at
    # S2-S3, which is where design documents are actually written.
    ("numeric-claim", "guard",    _is_claim_doc, g_numeric_claim,  True),
    ("evidence-path", "strict",   None,          None,             False),
]

LEVEL_RANK = {"advisory": 0, "guard": 1, "strict": 2}


def implemented_guards(enforcement):
    """Guards that actually run at this enforcement level.

    The digest reads this rather than a hand-written list, so it can never
    advertise a guard that does not exist (invariant I2, applied to ourselves).
    """
    rank = LEVEL_RANK.get(enforcement, 0)
    return [g[0] for g in REGISTRY if g[4] and LEVEL_RANK[g[1]] <= max(rank, 1)]


def run(path, text, ctx):
    findings = []
    for gid, level, applies, fn, impl in REGISTRY:
        if not impl or applies is None or not applies(path):
            continue
        try:
            for f in fn(path, text, ctx) or []:
                f["level"] = level
                findings.append(f)
        except Exception as exc:                   # noqa: BLE001
            findings.append(_f(gid, f"guard raised {type(exc).__name__}: {exc} "
                                    f"- treat as unchecked, not as clean",
                               "guards.py", None))
    return findings


def decide(findings, enforcement):
    """advisory never denies; guard denies guard-level; strict denies everything.

    Non-denied findings are still surfaced as context, so a Stage 1 project is
    warned without being blocked.
    """
    rank = LEVEL_RANK.get(enforcement, 0)
    deny, warn = [], []
    for f in findings:
        if rank == 0:
            warn.append(f)
        elif LEVEL_RANK[f.get("level", "guard")] <= rank:
            deny.append(f)
        else:
            warn.append(f)
    return deny, warn
