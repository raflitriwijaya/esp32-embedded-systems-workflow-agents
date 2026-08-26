# idf_mcp_launch.ps1 — launch the ESP-IDF Tools MCP server with a clean stdout.
#
# `idf.py mcp-server` speaks JSON-RPC over stdio. Anything else written to stdout
# corrupts the stream, and ESP-IDF's export script is chatty. This wrapper sends
# every byte of activation output to stderr or to null, so the server owns stdout
# alone.
#
# Register with:
#   claude mcp add --transport stdio esp-idf -- powershell -NoProfile `
#     -ExecutionPolicy Bypass -File "%USERPROFILE%\.claude\tools\idf_mcp_launch.ps1" `
#     -ProjectDir "<path to the ESP-IDF project>"
#
# Requires the `mcp` Python package inside the ESP-IDF virtualenv. Espressif ship
# it as the EIM installer's "mcp" feature; without it `idf.py mcp-server` exits
# with "MCP dependencies not available".

[CmdletBinding()]
param([string]$ProjectDir = ".")

$ErrorActionPreference = 'Stop'
try { $env:PYTHONIOENCODING = 'utf-8' } catch { }

function Resolve-IdfPath {
    if ($env:IDF_PATH -and (Test-Path (Join-Path $env:IDF_PATH 'tools\idf.py'))) { return $env:IDF_PATH }
    if ($env:ESP_IDF_ROOT -and (Test-Path (Join-Path $env:ESP_IDF_ROOT 'tools\idf.py'))) { return $env:ESP_IDF_ROOT }
    foreach ($r in @('C:\esp', "$env:USERPROFILE\esp", 'C:\Espressif\frameworks')) {
        if (Test-Path $r) {
            $hits = Get-ChildItem $r -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $c = Join-Path $_.FullName 'esp-idf'
                if (Test-Path (Join-Path $c 'tools\idf.py')) { $c }
                elseif (Test-Path (Join-Path $_.FullName 'tools\idf.py')) { $_.FullName }
            }
            if ($hits) { return ($hits | Sort-Object -Descending | Select-Object -First 1) }
        }
    }
    return $null
}

function Resolve-IdfToolsPath {
    # activate.py locates the managed virtualenv and the toolchain through
    # IDF_TOOLS_PATH. Without it the export produces nothing, no idf.py function
    # is defined, and the failure surfaces far away as "idf.py is not
    # recognized". Resolve it the same way IDF_PATH is resolved - never assume
    # the Windows default, which is NOT where every installer puts it.
    if ($env:IDF_TOOLS_PATH -and (Test-Path (Join-Path $env:IDF_TOOLS_PATH 'python_env'))) {
        return $env:IDF_TOOLS_PATH
    }
    foreach ($c in @('C:\Espressif', "$env:USERPROFILE\.espressif")) {
        if ((Test-Path (Join-Path $c 'python_env')) -or (Test-Path (Join-Path $c 'idf-env.json'))) {
            return $c
        }
    }
    return $null
}

$idf = Resolve-IdfPath
if (-not $idf) {
    [Console]::Error.WriteLine("[idf-mcp] ESP-IDF not found; set IDF_PATH")
    exit 1
}

# *> $null captures every stream, including Write-Host, which -NoProfile alone
# would not suppress. This is the reason this wrapper exists: any stray byte on
# stdout corrupts the JSON-RPC stream.
# PowerShell 5.1 wraps a native command's stderr in NativeCommandError, which
# under ErrorActionPreference='Stop' terminates even when the exe exits 0.
# export.ps1 shells out to python, so activation MUST run with Continue or the
# launcher dies before the server starts. This failed in testing exactly here.
$tools = Resolve-IdfToolsPath
if ($tools) { $env:IDF_TOOLS_PATH = $tools }

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
# Dot-sourced, not called with '&': the call operator runs export.ps1 in a
# child scope and its PATH/IDF_PATH changes die with it, leaving idf.py
# unresolvable. This also failed in testing exactly here.
. (Join-Path $idf 'export.ps1') *> $null
$ErrorActionPreference = $prevEAP

$proj = (Resolve-Path $ProjectDir).Path
[Console]::Error.WriteLine("[idf-mcp] idf=$idf project=$proj")

# exec-style: idf.py inherits stdin/stdout untouched from here on.
& idf.py -C $proj mcp-server
exit $LASTEXITCODE
