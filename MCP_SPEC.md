# MCP_SPEC — Evidence Wiring (Phase 3)

**Purpose:** connect the kernel to the tools that produce tier-E0 evidence — observations from the real toolchain and the real device — and to the vendor documentation that settles tier-E2 questions without relying on model memory.

Everything below was verified against the installation on this machine, not against a blog post. Where the two disagreed, the installation won.

---

## 1. What was found on this machine

| Fact | Value | How it was established |
|---|---|---|
| ESP-IDF | v6.0.2 | `esp_idf_version.h` (`MAJOR 6 / MINOR 0 / PATCH 2`) |
| IDF root | `C:\esp\v6.0.2\esp-idf` | filesystem |
| `version.txt` | **absent** | filesystem — the version fallback chain exists precisely for this |
| Toolchain root | `C:\Espressif` | `espidf.constraints.v6.0.txt` |
| IDF virtualenv | `C:\Espressif\python_env\idf6.0_py3.14_env` | filesystem; PyYAML 6.0.3 present |
| `idf.py mcp-server` | **present** | `tools/idf_py_actions/mcp_ext.py`, action name `mcp-server` |
IDF_TOOLS_PATH=C:\Espressif
python <IDF>\tools\idf_tools.py --idf-path <IDF> install-python-env --features=core,mcp
| `eim` CLI | present in Downloads, but **EIM has no record of this install** (`eim list` → "No versions found") | running EIM would have installed a *second* ESP-IDF, not added a feature to this one |
| `IDF_TOOLS_PATH` | `C:\Espressif` | `idf-env.json` location — **not** the Windows default `%USERPROFILE%\.espressif` |
| Declared optional features | `core, test-specific, ci, docs, ide, mcp` | `tools/requirements.json` in the install |

Espressif's published launch command (`eim run "idf.py mcp-server"`) does not work as written on this machine, and EIM would have been the wrong instrument anyway.

The correct mechanism was stated by the installation itself. `tools/requirements/requirements.mcp.txt` says:

> This feature can be enabled by running "install.{sh,bat,ps1,fish} --enable-mcp"

`install.ps1` also re-runs the full toolchain install, so only its Python step was used:

```
IDF_TOOLS_PATH=C:\Espressif
python <IDF>\tools\idf_tools.py --idf-path <IDF> \
  install-python-env --features=core,mcp
```

`core` is retained deliberately: `--features` sets the list rather than appending, and `idf-env.json` recorded only `core` beforehand. The install honoured `espidf.constraints.v6.0.txt`, so nothing escaped the managed environment.

---

## 2. The two servers

### 2.1 Espressif Documentation MCP — remote, no local dependency

| | |
|---|---|
| Transport | HTTP |
| Endpoint | `https://mcp.espressif.com/docs` |
| Tool | `search_espressif_sources(query, language)` |
| Covers | datasheets, TRM, hardware design guidelines, ESP-IDF Programming Guide, advisories, PCNs |
| Limits | 40 requests/hour, 200/day per user |

Register:

```
claude mcp add --transport http espressif-docs https://mcp.espressif.com/docs
```

**Role in the evidence model.** This is the tier-**E2** source. It settles "what does this API do in this version" against the vendor's own text instead of model recall — the direct countermeasure to the H1 and H5 failure classes that produced this project's `v5.3` pin and its bytes-versus-words error.

The rate limit is low enough to matter: it is a lookup of last resort for a specific question, not a background reference to consult on every turn.

**Registered at user scope. Requires OAuth.** After registration `claude mcp list` reports *Needs authentication*; the browser flow must be completed once from an interactive session with `/mcp`. Until then the server is registered but unusable, and any question it would have settled stays tier E3.

### 2.2 ESP-IDF Tools MCP — local, stdio, verified working

Read from `mcp_ext.py` in the installed tree, not from documentation:

| Kind | Name | Notes |
|---|---|---|
| tool | `build_project()` | |
| tool | `set_target(target)` | |
| tool | `flash_project(port=None)` | **mutates hardware** — see §4 |
| tool | `clean_project()` | |
| resource | `project://config` | project configuration and build directory |
| resource | `project://status` | build status, current target, IDF version, artifact presence |
| resource | `project://devices` | connected serial ports |

**Status: verified working.** A JSON-RPC handshake through `idf_mcp_launch.ps1` returns:

```
initialize -> serverInfo {"name": "ESP-IDF", "version": "1.29.1"}
tools      -> build_project, clean_project, flash_project, set_target
resources  -> project://config, project://devices, project://status
non-JSON bytes on stdout: 0
```

The tool list matches what was read from `mcp_ext.py`, and the zero junk count is the launcher's central claim holding under test.

### Four defects the handshake exposed

None of these would have announced themselves. A registered-but-broken server looks configured.

| # | Defect | Why it happened |
|---|---|---|
| 1 | Running `idf.py` from Git Bash | `idf.py` refuses MSys/Mingw outright. The launcher is PowerShell-only for this reason |
| 2 | `$ErrorActionPreference = 'Stop'` around activation | PowerShell 5.1 wraps a native command's stderr in `NativeCommandError`; `export.ps1` shells out to python, so activation terminated even at exit code 0. Fixed by switching to `Continue` across the export and restoring afterwards |
| 3 | `& export.ps1` instead of `. export.ps1` | The call operator runs it in a child scope, so `PATH` and `IDF_PATH` died with it. `idf.py` then appeared "not recognized" — a symptom pointing nowhere near the cause |
| 4 | `IDF_TOOLS_PATH` never set | `activate.py --export` locates the venv through it. Unset, it emits nothing, no `idf.py` **function** is defined, and the failure again surfaces as "not recognized". This machine uses `C:\Espressif`, not the Windows default |

Defects 3 and 4 produced an identical error message from unrelated causes. That is the argument for the handshake probe over a smoke test: it distinguishes "did not start" from "started and answered".

Note that `idf.py` is a **PowerShell function** defined by the export, not an executable on `PATH`. Anything invoking it must dot-source the activation into its own scope first.

Launch:

```
claude mcp add --transport stdio esp-idf -- powershell -NoProfile -ExecutionPolicy Bypass ^
  -File "%USERPROFILE%\.claude\tools\idf_mcp_launch.ps1" -ProjectDir "<project>"
```

`idf_mcp_launch.ps1` exists because `idf.py mcp-server` speaks JSON-RPC over **stdio** while `export.ps1` writes freely to stdout. The wrapper redirects every activation stream with `*> $null` so the server owns stdout alone. Without it the stream is corrupted by the environment setup that precedes it.

### 2.3 Scope — the Tools server is project-scoped by nature

Registering the Tools server at user scope was tried and **reverted**. `idf.py mcp-server` refuses to start outside a project:

```
[idf-mcp] idf=C:\esp6.0.2\esp-idf project=<some non-project directory>
Open the MCP server in a valid ESP-IDF project directory.
```

A user-scoped registration therefore reports *Failed to connect — CONNECTION_CLOSED* in every directory that is not an ESP-IDF project. That is not a defect to work around: the underlying tool is project-bound, so the registration must be too. A registration that permanently shows as broken teaches you to ignore the health display, which is worse than not registering at all.

**Register it per project**, from inside the project directory:

```
claude mcp add --transport stdio esp-idf -- powershell -NoProfile -ExecutionPolicy Bypass ^
  -File "<framework>\tools\idf_mcp_launch.ps1"
```

Default scope is `local`, which is what is wanted here. No `-ProjectDir` is needed: the launcher defaults to the working directory, so the server binds to the project it was registered from.

This belongs in the new-project bootstrap alongside copying `stage-state.template.yaml`.

### 2.4 Stdout cleanliness, measured

The launcher's central claim was tested rather than asserted. In a valid project the handshake reports **0 non-JSON lines on stdout**. A stray `Executing action: mcp-server` does appear on stdout — but only on the failure path, where no JSON-RPC session exists to corrupt.

---

## 3. What MCP does not cover — and why the Bash path stays

The Tools server exposes four actions. It exposes **none** of the commands that produce the measurements the stage gates actually require:

| Evidence required by | Command | On the MCP server? |
|---|---|---|
| Flash/RAM budget | `idf.py size --format json2 --output-file …` | No |
| Per-component footprint | `idf.py size-components` | No |
| Crash analysis, reset cause | `idf.py coredump-info` | No |
| Stack high-water-mark, heap trend, 72-hour run | `idf.py monitor` | No |

So MCP is the *action* channel and a narrow Bash allowlist is the *measurement* channel. Both are needed; neither replaces the other.

Recommended allowlist — read-only subcommands only:

```json
{ "permissions": { "allow": [
  "Bash(python */tools/stage_kernel.py *)",
  "Bash(*/idf_run.ps1 * size*)",
  "Bash(*/idf_run.ps1 * size-components*)",
  "Bash(*/idf_run.ps1 * coredump-info*)",
  "Bash(*/idf_run.ps1 * build*)"
] } }
```

`flash`, `erase-flash`, `monitor` and `set-target` are deliberately **excluded**. The first two mutate hardware; `monitor` holds the serial port open and is interactive; `set-target` rewrites `sdkconfig` and would invalidate the derived cache underneath a running session. These stay manual, which is the human-in-the-loop point rather than an oversight.

---

## 4. Actions that change hardware

`flash_project(port)` and anything reaching `idf.py flash` or `erase-flash` are never auto-approved, at any enforcement level.

The MCP form is the safer of the two available: the port is a typed parameter rather than a string assembled into a shell command. That matters at Stage 4, where ten pilot units may be attached at once and the wrong port is a real and expensive mistake.

---

## 5. How this closed a gap the digest had been reporting

From its first run, the digest listed `build_warnings.<target>` under `not_known`, because nothing archived a build log. Phase 3 closes it:

1. `idf_run.ps1 -Target esp32s3 build` tees output to `tests/reports/build-<target>-<date>.log`
2. The cache generator reads the newest matching log and counts `warning:` and `error:` lines
3. `last_build` gains `warnings`, `errors`, and `log` — each with the path it came from
4. `build_warnings` leaves `not_known`
5. A non-zero count raises an inline digest warning citing the Gate 2→3 zero-warning criterion

The number is never inferred. With no log the field stays `null` and the item stays in `not_known` — it does not become zero. Reporting zero warnings for a build that was never captured would be exactly the fabricated-evidence failure (H6) this framework was built to prevent.

`idf.py size --format json2 --output-file tests/reports/size-<target>.json` produces the same kind of machine-readable evidence for the flash and RAM budget; wiring it into `observations[]` belongs to the claim-closure loop in phase 5.

---

## 6. Evidence tiers after phase 3

| Tier | Source | Wired |
|---|---|---|
| **E0** observed on target | build logs via `idf_run.ps1`; `size`, `coredump-info`, `monitor` via the allowlist; `project://status` once the Tools MCP is unblocked | Partial — build logs now, device measurements in phase 5 |
| **E1** repo artifact | `sdkconfig`, `project_description.json`, `CMakeLists.txt`, `dependencies.lock` | Yes, since phase 1 |
| **E2** pinned vendor doc | Espressif Documentation MCP | Ready to register |
| **E3** model prior | — | Never an answer; becomes an `ASM-` entry |

---

## 7. Install summary

```
# E2 — no local dependency, register now
claude mcp add --transport http espressif-docs https://mcp.espressif.com/docs

# E0 actions — only after the mcp package is available in the IDF venv
claude mcp add --transport stdio esp-idf -- powershell -NoProfile -ExecutionPolicy Bypass ^
  -File "%USERPROFILE%\.claude\tools\idf_mcp_launch.ps1" -ProjectDir "<project>"
```

Both commands write to the user's Claude configuration. Neither is run by the framework on its own.
