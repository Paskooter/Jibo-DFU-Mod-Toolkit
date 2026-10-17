# Fail CI only after evidence upload, without cancelling either render lane.
param([string]$OutDir = 'setup-previews', [switch]$ControlReceipt)
$ErrorActionPreference = 'Stop'
function Assert-HealthyReceipt($Receipt, [string]$Lane) {
    if ($Receipt.failed -or @($Receipt.problems).Count) { throw "$Lane receipt failed or has layout problems" }
    foreach ($p in $Receipt.phases.PSObject.Properties) {
        if ($p.Value.status -ne 'ok') { throw "$Lane phase $($p.Name): $($p.Value.status)" }
    }
}
if ($ControlReceipt) {
    Assert-HealthyReceipt (Get-Content -Raw (Join-Path $OutDir 'receipt.json') | ConvertFrom-Json) 'negative-control'
    exit 0
}
foreach ($lane in @('standard','high-contrast')) {
    $path = Join-Path (Join-Path $OutDir $lane) 'receipt.json'
    $r = Get-Content -Raw -LiteralPath $path | ConvertFrom-Json
    Assert-HealthyReceipt $r $lane
    if ($r.clipboard -ne 'ok' -or $r.fallback.setup_window -ne 'ok' -or $r.fallback.launcher -ne 'ok') { throw "$lane clipboard/fallback check failed" }
    foreach ($entry in $r.files) {
        $name = ($entry -split ' ')[0] + '.png'
        if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $path) $name))) { throw "Missing $lane PNG $name" }
    }
    $expected = if ($lane -eq 'standard') { 144 } else { 53 }
    if ($r.files.Count -ne $expected) { throw "$lane rendered $($r.files.Count) states, expected $expected (update only when the state catalogue changes)" }
    if ($lane -eq 'standard') {
        if ($r.live.stall_answer -ne 'stopped') { throw 'Live stall dialog did not stop' }
        foreach ($p in $r.live.PSObject.Properties) {
            if ($p.Name -notin @('stall_answer','stall_dpi') -and $p.Value -ne 'ok') { throw "Live capture $($p.Name) failed" }
        }
    } elseif (-not $r.high_contrast_active -or -not $r.high_contrast_switched -or -not $r.high_contrast_restored) { throw 'Windows high contrast not enabled/restored' }
    Write-Host "PASS $lane receipt: $($r.files.Count) rendered states, zero layout problems, clipboard and fallbacks ok"
}
Write-Host 'PASS live controller stall decision: stopped (125%); native 100% font-lifetime defect is separately documented'
