# idf_run.ps1 — run an idf.py subcommand with the ESP-IDF environment activated,
# and archive the output as tier-E0 evidence.
#
#   .\idf_run.ps1 -Target esp32s3 build
#   .\idf_run.ps1 -Target esp32s3 size --format json2 --output-file tests/reports/size-esp32s3.json
#   .\idf_run.ps1 monitor
#
# A build run is written to tests/reports/build-<target>-<date>.log, which is
# where stage_kernel.py looks for the warning count. Without that file the digest
# reports build_warnings as unknown - it never assumes zero.
#
# The IDF location is RESOLVED, never hardcoded (invariant I2): IDF_PATH first,
# then ESP_IDF_ROOT, then the newest install under the usual roots. If none is
# found the script says so and stops rather than guessing.

[CmdletBinding()]
param(
    [string]$Target,
    [string]$ProjectDir = ".",
    [switch]$NoArchive,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$IdfArgs
)

$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

function Resolve-IdfPath {
    if ($env:IDF_PATH -and (Test-Path (Join-Path $env:IDF_PATH 'tools\idf.py'))) { return $env:IDF_PATH }
    if ($env:ESP_IDF_ROOT -and (Test-Path (Join-Path $env:ESP_IDF_ROOT 'tools\idf.py'))) { return $env:ESP_IDF_ROOT }
    $roots = @('C:\esp', "$env:USERPROFILE\esp", 'C:\Espressif\frameworks')
    $found = foreach ($r in $roots) {
        if (Test-Path $r) {
            Get-ChildItem $r -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $c = Join-Path $_.FullName 'esp-idf'
                if (Test-Path (Join-Path $c 'tools\idf.py')) { $c }
                elseif (Test-Path (Join-Path $_.FullName 'tools\idf.py')) { $_.FullName }
            }
        }
    }
    if ($found) { return ($found | Sort-Object -Descending | Select-Object -First 1) }
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
    Write-Error ("ESP-IDF not found. Set IDF_PATH, or install under C:\esp. " +
                 "This script does not guess an install location.")
    exit 1
}

$export = Join-Path $idf 'export.ps1'
if (-not (Test-Path $export)) { Write-Error "export.ps1 missing under $idf"; exit 1 }

# Activation chatter must not mix into the command output that gets archived.
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
. $export *> $null
$ErrorActionPreference = $prevEAP

$proj = (Resolve-Path $ProjectDir).Path
$argList = @()
if ($Target) { $argList += @('-B', (Join-Path $proj ("build_" + $Target))) }
$argList += $IdfArgs

Write-Host ("[idf_run] " + $idf + "  ->  idf.py " + ($argList -join ' '))

$isBuild = ($IdfArgs -contains 'build')
$log = $null
if ($isBuild -and -not $NoArchive) {
    $t = if ($Target) { $Target } else { 'unknown' }
    $dir = Join-Path $proj 'tests\reports'
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $log = Join-Path $dir ("build-" + $t + "-" + (Get-Date -Format 'yyyy-MM-dd') + ".log")
}

Push-Location $proj
try {
    if ($log) {
        & idf.py @argList 2>&1 | Tee-Object -FilePath $log
        Write-Host ("[idf_run] archived -> " + $log)
    } else {
        & idf.py @argList
    }
    $code = $LASTEXITCODE
} finally { Pop-Location }

if ($log) {
    # Refresh the derived cache so the digest reflects this run immediately.
    $kernel = Join-Path $PSScriptRoot 'stage_kernel.py'
    if (Test-Path $kernel) { & python $kernel cache -C $proj | Out-Null }
}
exit $code
