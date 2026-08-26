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

# export.ps1 re-derives the whole environment through activate.py on every
# launch, which measured 7.9 s to first JSON-RPC response. MCP clients give up
# well before that and report CONNECTION_CLOSED, so the server looked broken
# while working perfectly under a patient probe.
#
# Cache the environment export and replay it. The cache is keyed on the IDF path
# and version and is discarded if any cached PATH entry has disappeared, so a
# toolchain change cannot be served from a stale snapshot.
$cache = Join-Path $PSScriptRoot ".idf_env_cache.json"
$verFile = Join-Path $idf "tools\cmake\version.cmake"
$ver = ""
if (Test-Path $verFile) {
    $ver = ((Select-String -Path $verFile -Pattern 'IDF_VERSION_[A-Z]+\s+(\d+)').Matches |
            ForEach-Object { $_.Groups[1].Value }) -join '.'
}

$applied = $false
if (Test-Path $cache) {
    try {
        $c = Get-Content $cache -Raw | ConvertFrom-Json
        $pathsOk = $true
        foreach ($d in ($c.PathHead -split ';')) {
            if ($d -and -not (Test-Path $d)) { $pathsOk = $false; break }
        }
        if ($c.IdfPath -eq $idf -and $c.Version -eq $ver -and $pathsOk) {
            foreach ($kv in $c.Env.PSObject.Properties) {
                Set-Item -Path ("Env:" + $kv.Name) -Value $kv.Value -ErrorAction SilentlyContinue
            }
            $applied = $true
            [Console]::Error.WriteLine("[idf-mcp] environment restored from cache")
        }
    } catch { }
}

if (-not $applied) {
    # Dot-sourced, not called with '&': the call operator runs export.ps1 in a
    # child scope and its PATH/IDF_PATH changes die with it, leaving idf.py
    # unresolvable.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    . (Join-Path $idf 'export.ps1') *> $null
    $ErrorActionPreference = $prevEAP

    $keep = @{}
    foreach ($n in @('IDF_PATH','IDF_TOOLS_PATH','IDF_PYTHON_ENV_PATH','ESP_IDF_VERSION',
                     'PATH','IDF_CCACHE_ENABLE','ESP_ROM_ELF_DIR','OPENOCD_SCRIPTS')) {
        $v = [Environment]::GetEnvironmentVariable($n)
        if ($v) { $keep[$n] = $v }
    }
    try {
        @{ IdfPath = $idf; Version = $ver; Env = $keep
           PathHead = (($env:PATH -split ';' | Where-Object { $_ -like "*Espressif*" }) -join ';') } |
          ConvertTo-Json -Depth 4 | Out-File $cache -Encoding utf8 -NoNewline
        [Console]::Error.WriteLine("[idf-mcp] environment exported and cached")
    } catch {
        [Console]::Error.WriteLine("[idf-mcp] export succeeded but the cache could not be written")
    }
}

$proj = (Resolve-Path $ProjectDir).Path
[Console]::Error.WriteLine("[idf-mcp] idf=$idf project=$proj")

# Invoke the script explicitly rather than the bare name 'idf.py'.
#
# export.ps1 defines idf.py as a PowerShell FUNCTION. Replaying a cached
# environment restores variables but not functions, so '& idf.py' then falls
# through to PATH - and PATH may still carry a stale idf-exe shim from an older
# .espressif install. That shim accepts the arguments, exits 0 after a second,
# and the client reports CONNECTION_CLOSED. Naming the script removes both the
# shim and any dependency on the function existing.
$pyExe = $null
if ($env:IDF_PYTHON_ENV_PATH) {
    $c = Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts\python.exe'
    if (Test-Path $c) { $pyExe = $c }
}
if (-not $pyExe) { $pyExe = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $pyExe) { [Console]::Error.WriteLine('[idf-mcp] no python interpreter'); exit 1 }

$idfPy = Join-Path $idf 'tools\idf.py'
[Console]::Error.WriteLine("[idf-mcp] $pyExe $idfPy -C $proj mcp-server")

# exec-style: python inherits stdin/stdout untouched from here on.
& $pyExe $idfPy -C $proj mcp-server
exit $LASTEXITCODE
