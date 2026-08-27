# PostToolUse hook - states what a write did to the build evidence.
#
# Advisory only. PostToolUse cannot block (the tool already ran), which is
# exactly right here: the point is that the model knows the archived log no
# longer describes the tree, not that the write is prevented.
#
# Always exits 0. A hook that crashes must fail OPEN.
#
# Register in ~/.claude/settings.json:
#   "PostToolUse": [
#     { "matcher": "Write|Edit",
#       "hooks": [ { "type": "command",
#         "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\post_tool_use_build.ps1\"" } ] }
#   ]

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

    # Cheap bail-out before paying for a Python start. Only files the ESP-IDF
    # build consumes matter; a markdown edit changes nothing about the log.
    if ($payload -notmatch '\.(c|h|cpp|hpp|cc|cxx|s)"' -and
        $payload -notmatch 'sdkconfig' -and
        $payload -notmatch 'CMakeLists' -and
        $payload -notmatch 'partitions') { exit 0 }

    $tool = Join-Path $PSScriptRoot '..\tools\stage_kernel.py'
    if (-not (Test-Path $tool)) { $tool = Join-Path $PSScriptRoot 'stage_kernel.py' }
    if (-not (Test-Path $tool)) { exit 0 }

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

    $out = $payload | & $py $tool post-tool-use
    if ($out) { $out -join "`n" | Write-Output }
}
catch {
    Write-Diag $_.Exception.Message
}
exit 0
