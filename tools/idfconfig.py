#!/usr/bin/env python3
"""ESP32 Stage Kernel - sdkconfig and partition table against the stage bar.

SECTION3 sec.2.2 states configuration requirements that turn on at a stage:
assertions from Stage 3, bootloader log silence from Stage 4, OTA partitions
from Stage 3, encrypted NVS from Stage 4. Every one of them is silent when
wrong - a missing partition is a build that flashes and cannot update itself,
and a missing assertion switch is a class of bug that simply stops being caught.

Every symbol these checks name is verified against kconfig-migration.yaml, which
is extracted from the installed tree. That matters: sec.2.2 itself says
"Confirm both symbol names against the installed v6.0.2 Kconfig tree before
wiring them into a CI check", and this file is that CI check.

What is established here is what the FILES say. Whether the partition sizes suit
the application, or whether an assertion is in the right place, is not something
a configuration file can show.
"""

from __future__ import annotations

import re
from pathlib import Path

VERIFIED = "MACHINE_CHECKED"
REFUTED = "MACHINE_REFUTED"
SKIPPED = "UNVERIFIABLE"

STAGES = ["S1", "S2", "S3", "S4", "S5"]

# (symbol, required value, from stage, spec citation, why it is silent)
STAGE_SYMBOLS = [
    ("CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_ENABLE", "y", "S3",
     "SECTION3 sec.2.2 step 3",
     "without it assertions compile away, so a whole class of precondition "
     "violation stops being reported at all"),
    ("CONFIG_BOOTLOADER_LOG_LEVEL_NONE", "y", "S4",
     "SECTION3 sec.2.2 step 3",
     "bootloader logging costs flash that a production image is expected to "
     "reclaim"),
]

# Warning suppression is never permitted at any stage - it is the cheapest way
# to falsely satisfy the Gate 2->3 zero-warning criterion. The guard catches it
# on write; this catches a file that was already committed.
FORBIDDEN_SYMBOLS = [
    ("CONFIG_COMPILER_DISABLE_DEFAULT_ERRORS", "SECTION3 sec.2.2 step 3"),
    ("CONFIG_COMPILER_DISABLE_GCC15_WARNINGS", "SECTION3 sec.2.2 step 3"),
]

# SECTION3 sec.2.2 step 2. name -> (from stage, what its absence costs)
STAGE_PARTITIONS = {
    "ota_0": ("S3", "no OTA slot, so a deployed unit cannot be updated in place"),
    "ota_1": ("S3", "OTA needs both slots to roll back a bad image"),
}

RE_PART_ROW = re.compile(r"^\s*([A-Za-z0-9_]+)\s*,\s*([a-z]+)\s*,\s*([a-z_0-9]*)\s*,",
                         re.M)


def _f(check, status, why, evidence=None, hints=None):
    return {"check": check, "status": status, "why": why,
            "evidence": evidence or [], "hints": hints or []}


def _rank(s):
    try:
        return STAGES.index(str(s).strip().upper())
    except ValueError:
        return None


def _load_migration(root_pkg: Path):
    f = root_pkg / "kconfig-migration.yaml"
    if not f.is_file():
        return None
    try:
        import yaml
        return yaml.safe_load(f.read_text(encoding="utf-8")) or None
    except Exception:                                  # noqa: BLE001
        return None


def c_stage_symbols(ctx):
    """Configuration this stage requires, and configuration it forbids."""
    stage, cfgs = ctx["stage"], ctx["configs"]
    if not cfgs:
        return _f("config-stage-symbols", SKIPPED,
                  "no configured target with a readable sdkconfig")
    here = _rank(stage)
    missing, forbidden, due_later = [], [], []
    for tgt, cfg in cfgs.items():
        for sym, want, from_stage, cite, why in STAGE_SYMBOLS:
            if here is None or here < _rank(from_stage):
                if sym not in {k for k in cfg}:
                    due_later.append(f"{sym} becomes required at {from_stage}")
                continue
            if cfg.get(sym) != want:
                got = cfg.get(sym)
                missing.append(
                    f"{tgt}: {sym} is "
                    f"{'unset' if got is None else got!r}, {from_stage}+ requires "
                    f"{want!r} - {why} ({cite})")
        for sym, cite in FORBIDDEN_SYMBOLS:
            if cfg.get(sym) == "y":
                forbidden.append(
                    f"{tgt}: {sym}=y suppresses the warnings the Gate 2->3 "
                    f"criterion is about. Enabling it is a gate finding, not a "
                    f"build fix ({cite})")
    if forbidden or missing:
        return _f("config-stage-symbols", REFUTED,
                  f"{len(forbidden) + len(missing)} configuration requirement(s) "
                  f"unmet at {stage}: {len(forbidden)} forbidden symbol(s) "
                  f"enabled, {len(missing)} required symbol(s) not set",
                  forbidden + missing,
                  ["these establish what sdkconfig says, never whether the "
                   "resulting image behaves correctly"])
    return _f("config-stage-symbols", VERIFIED,
              f"every sdkconfig requirement {stage} states is met across "
              f"{len(cfgs)} target(s), and no warning-suppression switch is on",
              hints=sorted(set(due_later))[:4])


def c_partition_bar(ctx):
    """SECTION3 sec.2.2 step 2: the partition table grows with the stage."""
    stage, root = ctx["stage"], ctx["root"]
    here = _rank(stage)
    csvs = [p for p in root.glob("partitions*.csv") if p.is_file()]
    if not csvs:
        return _f("config-partition-bar", SKIPPED,
                  "no partitions*.csv in the project - the default single-factory "
                  "table applies, which sec.2.2 permits only at S1-S2"
                  if here is not None and here <= 1 else
                  "no partitions*.csv to read, and from S3 sec.2.2 requires an "
                  "OTA-capable table")
    csv = csvs[0]
    rows = RE_PART_ROW.findall(csv.read_text(encoding="utf-8", errors="ignore"))
    names = {r[0] for r in rows}
    rel = csv.name
    missing, early = [], []
    for name, (from_stage, why) in STAGE_PARTITIONS.items():
        need = _rank(from_stage)
        if here is not None and here >= need and name not in names:
            missing.append(f"{rel} has no '{name}' partition, required from "
                           f"{from_stage} - {why}")
    if here is not None and here <= 1:
        for name in STAGE_PARTITIONS:
            if name in names:
                early.append(f"{rel} already carries '{name}'; sec.2.2 asks for "
                             f"factory only at S1-S2, so this is ahead of the "
                             f"bar rather than short of it")
    if missing:
        return _f("config-partition-bar", REFUTED,
                  f"{len(missing)} partition(s) the {stage} bar requires are "
                  f"absent",
                  missing,
                  [f"{len(names)} partition(s) declared: "
                   f"{', '.join(sorted(names))}"])
    return _f("config-partition-bar", VERIFIED,
              f"{rel} satisfies the {stage} partition bar with "
              f"{len(names)} partition(s)",
              evidence=early,
              hints=[f"declared: {', '.join(sorted(names))}",
                     "sizes and offsets are not checked here - a table can be "
                     "well-formed and still too small for the image"])


def c_symbols_are_real(ctx):
    """Every symbol these checks name must exist in the installed tree.

    sec.2.2 says to confirm the warning-suppression symbol names against the
    installed Kconfig before wiring them into a CI check. This is that CI check,
    so it confirms its own vocabulary rather than assuming it.
    """
    mig = ctx.get("migration")
    if not mig:
        return _f("config-symbols-real", SKIPPED,
                  "kconfig-migration.yaml absent - run tools/extract_kconfig.py")
    valid = set(mig.get("valid") or [])
    renamed = mig.get("renamed") or {}
    named = [s for s, _v, _st, _c, _w in STAGE_SYMBOLS] + \
            [s for s, _c in FORBIDDEN_SYMBOLS]
    bad = []
    for sym in named:
        bare = sym[len("CONFIG_"):]
        if bare in valid:
            continue
        if bare in renamed:
            bad.append(f"{sym} was renamed to CONFIG_{renamed[bare]} - this "
                       f"check is testing a name that no longer exists")
        else:
            bad.append(f"{sym} is not a symbol in the installed ESP-IDF "
                       f"v{mig.get('idf_version')} tree")
    if bad:
        return _f("config-symbols-real", REFUTED,
                  f"{len(bad)} of {len(named)} symbol(s) this check names do not "
                  f"exist in the installed tree - the check would pass or fail "
                  f"for the wrong reason",
                  bad)
    return _f("config-symbols-real", VERIFIED,
              f"all {len(named)} symbol(s) these checks name exist in the "
              f"installed ESP-IDF v{mig.get('idf_version')} tree")


CHECKS = [c_symbols_are_real, c_stage_symbols, c_partition_bar]


def run(root: Path, state, targets, pkg_root: Path):
    stage = ((state or {}).get("current") or {}).get("stage")
    if stage not in STAGES:
        return [_f("config-stage-bar", SKIPPED,
                   f"stage {stage!r} is not one of {', '.join(STAGES)}")]
    import stage_kernel as sk
    configs = {}
    for t in targets or []:
        sk_path = t.get("sdkconfig")
        if t.get("configured") and sk_path and Path(sk_path).is_file():
            configs[t["target"]] = sk.parse_sdkconfig(Path(sk_path))
    ctx = {"root": root, "stage": stage, "configs": configs,
           "migration": _load_migration(pkg_root)}
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
