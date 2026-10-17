# Regression control: an actually hung child must be killed, receipted, and followed
# by another phase. Does not compile/run product UI or touch setup/USB/WSL.
param([string]$OutDir = 'setup-previews/harness-control')
$ErrorActionPreference = 'Stop'
$watch = [Diagnostics.Stopwatch]::StartNew()
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'render-setup-previews.ps1') -OutDir $OutDir -TimeoutProbe
if ($LASTEXITCODE -ne 1) { throw 'Injected timeout must fail the renderer' }
$r = Get-Content -Raw (Join-Path $OutDir 'receipt.json') | ConvertFrom-Json
if ($r.phases.'timeout-probe'.status -ne 'timeout') { throw 'Hung child was not recorded as timeout' }
if ($r.phases.'success-probe'.status -ne 'ok') { throw 'A timeout prevented the next phase from running' }
if ($watch.Elapsed.TotalSeconds -gt 30) { throw 'Timeout control exceeded its bounded budget' }
if (-not (Test-Path (Join-Path $OutDir 'progress.txt'))) { throw 'Missing incremental progress artifact' }
Write-Host 'PASS preview harness: killed hung child, recorded timeout, continued to next phase'
