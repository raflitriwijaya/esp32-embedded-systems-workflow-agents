# PreToolUse hook — ESP32 Stage Kernel layer 3 guards.
#
# Reads the hook event from stdin, forwards it to stage_kernel.py guard, and
# relays that decision on stdout. Always exits 0: a guard that crashes must fail
# OPEN (allow the write) rather than block all work, but it must also never
# report silence as cleanliness — the Python side emits an explicit
# "NOT checked" context line when its checks fail to run.
#
# Register in ~/.claude/settings.json:
#   "hooks": {
#     "PreToolUse": [
#       { "matcher": "Write|Edit",
#         "hooks": [ { "type": "command",
#           "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\pre_tool_use_guard.ps1\"" } ] }
#     ]
#   }

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

function Write-Diag([string]$m) { [Console]::Error.WriteLine("[stage-kernel] $m") }

try {
    $payload = [Console]::In.ReadToEnd()
    if (-not $payload) { exit 0 }

    $tool = Join-Path $PSScriptRoot '..\tools\stage_kernel.py'
    if (-not (Test-Path $tool)) { $tool = Join-Path $PSScriptRoot 'stage_kernel.py' }
    if (-not (Test-Path $tool)) { Write-Diag 'stage_kernel.py not found'; exit 0 }

    # Cheap bail-out before paying for a Python start: only source files and
    # sdkconfig are ever guarded, so most writes cost one substring test.
    if ($payload -notmatch '\.(c|h|cpp|hpp|cc|cxx|ino)"' -and
        $payload -notmatch 'sdkconfig') { exit 0 }

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

    $out = $payload | & $py $tool guard
    if ($out) { $out -join "`n" | Write-Output }
}
catch {
    Write-Diag $_.Exception.Message
}
exit 0
