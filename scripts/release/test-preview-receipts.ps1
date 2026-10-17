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
    $required = @('setup-96','support-96','fallback-setup','launcher-text','fallback-launcher','clipboard','font-control')
    if ($lane -eq 'standard') { $required += @('setup-144','setup-192','support-144','support-192','simulated-hc','scaled-forms','live') }
    foreach ($phase in $required) { if ($r.phases.PSObject.Properties.Name -notcontains $phase) { throw "$lane missing phase $phase" } }
    if ($r.font_lifetime_control -ne 'ok') { throw "$lane native font lifecycle failed or missing" }
    if ($r.clipboard -ne 'ok' -or $r.fallback.setup_window -ne 'ok' -or $r.fallback.launcher -ne 'ok') { throw "$lane clipboard/fallback check failed" }
    foreach ($entry in $r.files) {
        $name = ($entry -split ' ')[0] + '.png'
        if (-not (Test-Path -LiteralPath (Join-Path (Split-Path $path) $name))) { throw "Missing $lane PNG $name" }
    }
    $expected = if ($lane -eq 'standard') { 144 } else { 53 }
    if ($r.files.Count -ne $expected) { throw "$lane rendered $($r.files.Count) states, expected $expected (update only when the state catalogue changes)" }
    if ($lane -eq 'standard') {
        if ($r.scaled_forms -ne 'ok') { throw 'Scaled form containment/action checks failed or missing' }
        foreach ($kind in @('stall','failure')) {
            if ($r.live."${kind}_answer" -ne 'stopped' -or $r.live."${kind}_clipboard" -ne 'ok' -or $r.live."${kind}_layout" -ne 'ok' -or $r.live."${kind}_dialog" -ne 'ok') { throw "Native $kind copy/layout/stop failed or missing" }
            if ($null -eq $r.live."${kind}_forced_dpi" -or $r.live."${kind}_forced_dpi" -ne 0 -or $r.live."${kind}_dpi" -ne 96) { throw "Native $kind must use ForcedDpi=0, actual DPI=96" }
        }
        foreach ($p in $r.live.PSObject.Properties) {
            if ($p.Name -notmatch '_(answer|dpi)$' -and $p.Value -ne 'ok') { throw "Live capture $($p.Name) failed" }
        }
    } elseif (-not $r.high_contrast_active -or -not $r.high_contrast_switched -or -not $r.high_contrast_restored) { throw 'Windows high contrast not enabled/restored' }
    Write-Host "PASS $lane receipt: $($r.files.Count) rendered states, zero layout problems, clipboard and fallbacks ok"
}
Write-Host 'PASS native100 controller stall/failure: actual96, forced0, clipboard exact, stopped; repeated DPI fonts and constrained actions ok'
