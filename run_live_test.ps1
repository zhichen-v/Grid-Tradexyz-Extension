$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$runStamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$runOutput = "logs\mm_v2_economics_$runStamp.jsonl"
$runTimer = [Diagnostics.Stopwatch]::StartNew()
$runCode = 1

Write-Host 'Starting one 60-minute LIVE session. Startup includes a 60-second API quarantine.'
Write-Host "Output: $runOutput"
Write-Host 'Progress appears every 10 seconds. Ctrl+C requests bounded cleanup; keep this window open.'

try {
    & .\.venv\Scripts\python.exe -u .\run_volume_market_maker.py --config .\config\market_maker_v2\test_live_economics_60m.yaml --output $runOutput --authorize-bounded-flatten --progress
    $runCode = $LASTEXITCODE
}
finally {
    $runTimer.Stop()
    [ordered]@{
        output = $runOutput
        planned_seconds = 3600
        wall_seconds = $runTimer.Elapsed.TotalSeconds
        exit_code = $runCode
    } | ConvertTo-Json | Set-Content -LiteralPath "$runOutput.window.json" -Encoding utf8
    Write-Host "Session ended. Exit code: $runCode. Check the final position/open-order summary."
}
