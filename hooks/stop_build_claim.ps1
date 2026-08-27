# Stop hook - holds the turn when a build claim outruns its evidence.
#
# THE ONLY FAIL-CLOSED MECHANISM IN THIS FRAMEWORK.
#
# Every other hook here fails open: a crash allows the write, an unreadable file
# reports "not checked". This one can hold the engineer's turn, and the
# documented Stop contract carries no loop protection and no stop_hook_active
# field. So the Python side enforces its own limits - never twice in a row,
# never more than three times per session - and this script still exits 0 on
# any error of its own.
#
# Two escape hatches:
#   STAGE_KERNEL_NO_STOP=1        disables it entirely
#   .no-stage-governance in root  disables all of this framework, as elsewhere
#
# Exit 2 is what holds the turn; the stderr text is what Claude is shown.
#
# Register in ~/.claude/settings.json:
#   "Stop": [
#     { "hooks": [ { "type": "command",
#         "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"%USERPROFILE%\\.claude\\hooks\\stop_build_claim.ps1\"" } ] }
#   ]

Set-StrictMode -Version Latest

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    $env:PYTHONIOENCODING = 'utf-8'
} catch { }

try {
    $payload = [Console]::In.ReadToEnd()
    if (-not $payload) { exit 0 }
    if ($env:STAGE_KERNEL_NO_STOP) { exit 0 }

    # Cheap bail-out: no build or test claim in the message, nothing to weigh.
    # Deliberately looser than the Python matcher - this only decides whether
    # paying for a Python start is worth it.
    if ($payload -notmatch 'compil' -and $payload -notmatch 'build' -and
        $payload -notmatch 'warning' -and $payload -notmatch 'test') { exit 0 }

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
    if (-not $py) { exit 0 }

    # stderr is relayed verbatim: on a hold it is the reason Claude is shown,
    # and it must arrive as written. `2>&1` inside PowerShell wraps every line
    # of a native command's stderr in a NativeCommandError, which would put a
    # stack trace in the middle of the explanation. Capture to a file instead.
    $tmpIn  = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmpIn, $payload, [System.Text.UTF8Encoding]::new($false))
        $p = Start-Process -FilePath $py -ArgumentList @($tool, 'stop') `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardInput $tmpIn -RedirectStandardError $tmpErr `
            -RedirectStandardOutput ([System.IO.Path]::GetTempFileName())
        $code = $p.ExitCode
        if (Test-Path $tmpErr) {
            $text = [System.IO.File]::ReadAllText($tmpErr)
            if ($text.Trim()) { [Console]::Error.WriteLine($text.TrimEnd()) }
        }
    } finally {
        Remove-Item $tmpIn, $tmpErr -ErrorAction SilentlyContinue
    }
    if ($code -eq 2) { exit 2 }
}
catch {
    # A hold that happens because this script broke would be indefensible.
    [Console]::Error.WriteLine("[stage-kernel] stop hook error: $($_.Exception.Message)")
}
exit 0
