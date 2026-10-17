# Fail CI only after evidence upload, without cancelling either render lane.
param([string]$OutDir = 'setup-previews')
$ErrorActionPreference = 'Stop'
foreach ($lane in @('standard','high-contrast')) {
    $path = Join-Path (Join-Path $OutDir $lane) 'receipt.json'
    $r = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
    if ($r.failed -or @($r.problems).Count) { throw "$lane receipt failed or has layout problems" }
    foreach ($p in $r.phases.PSObject.Properties) {
        if ($p.Value.status -ne 'ok') { throw "$lane phase $($p.Name): $($p.Value.status)" }
    }
    if ($r.clipboard -ne 'ok' -or $r.fallback.setup_window -ne 'ok' -or $r.fallback.launcher -ne 'ok') { throw "$lane clipboard/fallback check failed" }
    foreach ($entry in $r.files) {
        $name = ($entry -split ' ')[0] + '.png'
        if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $path) $name))) { throw "Missing $lane PNG $name" }
    }
    if ($lane -eq 'standard') {
        if ($r.live.stall_answer -ne 'stopped') { throw 'Live stall dialog did not stop' }
        foreach ($p in $r.live.PSObject.Properties) {
            if ($p.Name -notin @('stall_answer','stall_dpi') -and $p.Value -ne 'ok') { throw "Live capture $($p.Name) failed" }
        }
    } elseif (-not $r.high_contrast_active -or -not $r.high_contrast_switched -or -not $r.high_contrast_restored) { throw 'Windows high contrast not enabled/restored' }
    Write-Host "PASS $lane receipt: $($r.files.Count) rendered states, zero layout problems, clipboard and fallbacks ok"
}
Write-Host 'PASS live controller stall decision: stopped (150%); native 100% font-lifetime defect is separately documented'
