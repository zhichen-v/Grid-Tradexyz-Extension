$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$runConfig = '.\config\market_maker_v2\test_live_economics_60m.yaml'
$configuredSeconds = & .\.venv\Scripts\python.exe -c 'import sys; from core.services.market_maker_v2.config import load_config; print(load_config(sys.argv[1]).session.duration_seconds)' $runConfig
if ($LASTEXITCODE -ne 0) { throw 'Cannot validate session configuration.' }
$plannedSeconds = [int]$configuredSeconds
$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$runOutput = "logs\mm_v2_economics_$runStamp.jsonl"
$runTimer = [Diagnostics.Stopwatch]::StartNew()
$runCode = 1

Write-Host "Starting one $($plannedSeconds / 60)-minute LIVE session. Startup includes a 60-second API quarantine."
Write-Host "Output: $runOutput"
Write-Host 'Key events appear immediately; heartbeat every 60 seconds. Ctrl+C requests bounded cleanup; keep this window open.'

try {
    $LASTEXITCODE = 1
    & .\.venv\Scripts\python.exe -u .\run_volume_market_maker.py --config $runConfig --output $runOutput --authorize-bounded-flatten --progress
    $runCode = $LASTEXITCODE
}
finally {
    # Ctrl+C can skip the statement after the native command, although Python
    # has completed its handler and PowerShell has recorded its actual exit code.
    $runCode = [int]$LASTEXITCODE
    $runTimer.Stop()
    [ordered]@{
        output = $runOutput
        planned_seconds = $plannedSeconds
        wall_seconds = $runTimer.Elapsed.TotalSeconds
        exit_code = $runCode
    } | ConvertTo-Json | Set-Content -LiteralPath "$runOutput.window.json" -Encoding utf8
    Write-Host "Session ended. Exit code: $runCode. Check the final position/open-order summary."
    exit $runCode
}
