# SessionStart hook — ESP32 Stage Kernel layer 1 digest.
#
# Contract: stdout of a SessionStart hook is added to Claude's context.
# Therefore this script prints the digest and NOTHING else. Any diagnostic
# goes to stderr, and the script always exits 0 — a broken hook must never
# break a session, and must never inject a partial or misleading digest.
#
# Register in ~/.claude/settings.json:
#   "hooks": {
#     "SessionStart": [
#       { "hooks": [ { "type": "command",
#           "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\session_start_digest.ps1\"" } ] }
#     ]
#   }

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# The digest contains non-ASCII punctuation. Windows consoles default to a
# legacy code page, which would turn injected context into mojibake. Pin both
# directions to UTF-8 before any child process runs.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

function Write-Diag([string]$m) { [Console]::Error.WriteLine("[stage-kernel] $m") }

try {
    $root = (Get-Location).Path
    $tool = Join-Path $PSScriptRoot '..\tools\stage_kernel.py'
    if (-not (Test-Path $tool)) {
        # Installed layout: hooks/ and tools/ side by side under ~/.claude/
        $tool = Join-Path $PSScriptRoot 'stage_kernel.py'
    }
    if (-not (Test-Path $tool)) { Write-Diag 'stage_kernel.py not found'; exit 0 }

    # Cheap pre-check: skip Python entirely for non-ESP-IDF directories.
    # This keeps the hook near-zero cost in every unrelated project, which is a
    # requirement of a user-level install.
    $isIdf = $false
    foreach ($n in @('sdkconfig', 'sdkconfig.defaults', 'stage-state.yaml')) {
        if (Test-Path (Join-Path $root $n)) { $isIdf = $true; break }
    }
    if (-not $isIdf) {
        $cml = Join-Path $root 'CMakeLists.txt'
        if (Test-Path $cml) {
            $t = Get-Content $cml -Raw -ErrorAction SilentlyContinue
            if ($t -and ($t -match 'IDF_PATH' -or $t -match 'idf_component_register')) {
                $isIdf = $true
            }
        }
    }
    if (-not $isIdf) { exit 0 }
    if (Test-Path (Join-Path $root '.no-stage-governance')) { exit 0 }

    # Interpreter: prefer the ESP-IDF managed venv, which is guaranteed to have
    # PyYAML because the component manager depends on it.
    $py = $null
    if ($env:IDF_PYTHON_ENV_PATH) {
        $c = Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts\python.exe'
        if (Test-Path $c) { $py = $c }
    }
    if (-not $py) {
        foreach ($cand in @('python', 'python3', 'py')) {
            $cmd = Get-Command $cand -ErrorAction SilentlyContinue
            if ($cmd) { $py = $cmd.Source; break }
        }
    }
    if (-not $py) { Write-Diag 'no Python interpreter found'; exit 0 }

    $out = & $py $tool digest -C $root
    if ($LASTEXITCODE -ne 0) { Write-Diag "digest exited $LASTEXITCODE"; exit 0 }
    if ($out) { $out -join "`n" | Write-Output }
}
catch {
    Write-Diag $_.Exception.Message
}
exit 0
