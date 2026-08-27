"""Extract the Kconfig symbol universe and the deprecation map, mechanically.

A list of "symbols removed in v6.0" written by hand is exactly the stale-able
fact invariant I2 warns against, and it would be wrong within one ESP-IDF
release. ESP-IDF already ships both halves:

  sdkconfig.rename*   50 files mapping a deprecated symbol to its replacement.
                      721 pairs, including CONFIG_SW_COEXIST_ENABLE ->
                      CONFIG_ESP_COEX_SW_COEXIST_ENABLE, which is the defect
                      SECTION2 and SECTION3 both carry.
  Kconfig*            every symbol the installed tree defines.

Symbols are harvested from `config` declarations AND from `select` / `depends
on` / `imply` references. ESP_COREDUMP_DATA_FORMAT_ELF is only ever selected,
never declared, and 130 symbols are like it - treating declarations alone as
the valid set would flag every one of them.

Regenerate after any ESP-IDF change; `idf_version` makes a stale copy
detectable and stage_kernel.py selftest compares it against the installed tree.
"""
import os
import pathlib
import re
import sys

OUT = pathlib.Path(__file__).resolve().parent.parent / "kconfig-migration.yaml"

RE_DECL = re.compile(r"^[ \t]*(?:menu)?config[ \t]+([A-Z0-9_]+)", re.M)
RE_REF = re.compile(r"^[ \t]*(?:select|depends on|imply)[ \t]+([A-Z0-9_]+)", re.M)
# ESP-IDF splits Kconfig across arbitrary suffixes - Kconfig.app_rollback,
# Kconfig.power, Kconfig.projbuild. A fixed name list missed those two and
# 190 rename targets looked absent because their declarations were never
# read. Glob the family instead.
KCONFIG_GLOB = "Kconfig*"


def idf_root():
    p = os.environ.get("IDF_PATH")
    return pathlib.Path(p) if p and pathlib.Path(p).is_dir() else None


def idf_version(idf: pathlib.Path):
    f = idf / "components" / "esp_common" / "include" / "esp_idf_version.h"
    if not f.is_file():
        return None
    t = f.read_text(encoding="utf-8", errors="ignore")
    parts = []
    for k in ("MAJOR", "MINOR", "PATCH"):
        m = re.search(rf"#define\s+ESP_IDF_VERSION_{k}\s+(\d+)", t)
        parts.append(m.group(1) if m else "?")
    return ".".join(parts)


def harvest(idf: pathlib.Path):
    """(valid symbols, {deprecated: replacement}, files read)."""
    # COMPILER_ASSERT_NDEBUG_EVALUATE and COMPILER_DISABLE_GCC15_WARNINGS live
    # in the top-level Kconfig, not under components/. Scanning components alone
    # made both look invented - including the two the warn-suppress guard rests
    # on. Read the top level too.
    roots = [idf / "components"] + [f for f in idf.glob("Kconfig*") if f.is_file()]
    decl, ref, renamed, n = set(), set(), {}, 0
    files = []
    for r in roots:
        files += list(r.rglob(KCONFIG_GLOB)) if r.is_dir() else [r]
    for f in files:
        if "managed_components" in f.parts or not f.is_file():
            continue
        n += 1
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        decl |= set(RE_DECL.findall(t))
        ref |= set(RE_REF.findall(t))
    for f in idf.rglob("sdkconfig.rename*"):
        if "managed_components" in f.parts:
            continue
        n += 1
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in t.splitlines():
            line = line.split("#")[0].strip()
            parts = line.split()
            if len(parts) == 2 and parts[0].startswith("CONFIG_") \
                    and parts[1].startswith("CONFIG_"):
                renamed.setdefault(parts[0], parts[1])
    return decl | ref, renamed, n


def main():
    idf = idf_root()
    if not idf:
        print("IDF_PATH is unset or not a directory - nothing written",
              file=sys.stderr)
        return 2
    ver = idf_version(idf)
    valid, renamed, nfiles = harvest(idf)
    if not valid:
        print(f"no Kconfig symbols found under {idf} - nothing written",
              file=sys.stderr)
        return 2

    # A rename target that no longer exists would send the engineer somewhere
    # that is not there. Check before writing rather than after.
    bad = {k: v for k, v in renamed.items()
           if v[len("CONFIG_"):] not in valid}
    L = ["# ESP-IDF Kconfig symbol universe and deprecation map.",
         "#",
         "# EXTRACTED MECHANICALLY from the installed tree by",
         "# tools/extract_kconfig.py. A hand-written list of removed symbols is",
         "# the stale-able fact invariant I2 warns against, and ESP-IDF already",
         "# ships both halves of this: sdkconfig.rename* for the deprecations,",
         "# Kconfig* for what exists.",
         "#",
         "# Symbols come from `config` declarations AND from `select` /",
         "# `depends on` / `imply` references. ESP_COREDUMP_DATA_FORMAT_ELF is",
         "# only ever selected, never declared, and it is not alone - taking",
         "# declarations as the valid set would flag every symbol like it.",
         "#",
         "# Names are stored WITHOUT the CONFIG_ prefix, as Kconfig writes them.",
         "",
         f"idf_path: {idf.as_posix()}",
         f"idf_version: {ver or 'null'}",
         f"kconfig_files_read: {nfiles}",
         ""]
    if bad:
        L += ["# Rename targets absent from the installed tree. Recorded rather",
              "# than dropped: a replacement that does not exist is a finding",
              "# about ESP-IDF, not a reason to stay silent about the rename.",
              "unresolved_renames:"]
        for k, v in sorted(bad.items())[:40]:
            L.append(f"  {k}: {v}")
        L.append("")
    L.append(f"renamed:   # {len(renamed)} deprecated -> replacement")
    for k, v in sorted(renamed.items()):
        L.append(f"  {k[len('CONFIG_'):]}: {v[len('CONFIG_'):]}")
    L += ["", f"valid:   # {len(valid)} symbols"]
    for s in sorted(valid):
        L.append(f"  - {s}")
    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")

    import yaml
    d = yaml.safe_load(OUT.read_text(encoding="utf-8"))
    print(f"wrote {OUT.name}")
    print(f"  IDF                : v{d['idf_version']} ({d['kconfig_files_read']} "
          f"files read)")
    print(f"  valid symbols      : {len(d['valid'])}")
    print(f"  deprecated renames : {len(d['renamed'])}")
    if bad:
        print(f"  ! rename targets not found in the tree: {len(bad)}")
    for probe, want in (("SW_COEXIST_ENABLE", False),
                        ("ESP_COEX_SW_COEXIST_ENABLE", True),
                        ("ESP32_TASK_WDT_TIMEOUT_S", False),
                        ("ESP_COREDUMP_DATA_FORMAT_ELF", True)):
        got = probe in set(d["valid"])
        flag = "ok" if got == want else "UNEXPECTED"
        print(f"    {probe:30} valid={got}  {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
