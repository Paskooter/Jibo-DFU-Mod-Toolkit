# Parse and exercise the bootstrap's pure helpers without WSL, USB, installs or UAC.
$ErrorActionPreference = 'Stop'
$path = Join-Path $PSScriptRoot 'bootstrap.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$functions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
$script:answer = 'n'
function Read-Host { param($Prompt); return $script:answer }
if (Confirm-Setup 'Test') { throw 'Decline was not respected' }
$script:answer = 'yes'
if (-not (Confirm-Setup 'Test')) { throw 'Explicit approval was not accepted' }
$script:received = @()
function wsl.exe { $script:received = $args; $global:LASTEXITCODE=0; return "Ubuntu`0" }
$result = Native -Arguments @('--distribution', 'Ubuntu with spaces', '--exec', 'true')
if ($result -ne 'Ubuntu' -or $script:received.Count -ne 4 -or $script:received[1] -ne 'Ubuntu with spaces') { throw 'Native argument or NUL handling failed' }
function wsl.exe { $global:LASTEXITCODE=1; return 'not installed' }
$failed = $false
try { Native -Arguments @('--version') | Out-Null } catch { $failed = $true }
if (-not $failed) { throw 'Native failure was swallowed' }
# Diagnose old glibc/musl distributions without running a real WSL process.
function wsl.exe { $global:LASTEXITCODE=0; return 'glibc 2.35' }
if (-not (Test-CompatibleDistribution 'fixture')) { throw 'Supported distro refused' }
function wsl.exe { $global:LASTEXITCODE=0; return 'glibc 2.31' }
if (Test-CompatibleDistribution 'fixture') { throw 'Old glibc accepted' }
function wsl.exe { $global:LASTEXITCODE=0; return 'musl' }
if (Test-CompatibleDistribution 'fixture') { throw 'musl distro accepted' }
# Original USB helper is shipped byte-for-byte, rather than a second USB algorithm.
$usbPath = Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'run.ps1'
$usbErrors=$null
[System.Management.Automation.Language.Parser]::ParseFile($usbPath, [ref]$tokens, [ref]$usbErrors) | Out-Null
if ($usbErrors.Count) { throw ($usbErrors | Out-String) }
# Exercise the entire missing-WSLg refusal path in a child process. A mock admin
# function leaves a sentinel if any installation is attempted after declining.
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-bootstrap-test-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
try {
    [IO.File]::WriteAllText((Join-Path $fixture 'payload.tar.gz'), 'not read on refusal')
    [IO.File]::WriteAllText((Join-Path $fixture 'run.ps1'), 'not run on refusal')
    $child = Join-Path $fixture 'refusal.ps1'
    $childText = @'
param($Bootstrap, $Payload, $Case)
$global:BootstrapTestCase = $Case
$global:BootstrapTestSentinel = Join-Path $Payload 'unexpected-admin'
function wsl.exe {
    if ($global:BootstrapTestCase -eq 'no-distro' -and $args[0] -eq '--version') {
        $global:LASTEXITCODE=0; return 'WSLg 1.0'
    }
    $global:LASTEXITCODE=1; return 'No installed distributions or GUI support'
}
function Read-Host { param($Prompt); return 'n' }
function Start-Process { [IO.File]::WriteAllText($global:BootstrapTestSentinel, 'failed'); throw 'Unexpected admin action' }
& $Bootstrap -PayloadRoot $Payload
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText($child, $childText)
    foreach ($case in @('no-gui', 'no-distro')) {
        $messages = @(& (Join-Path $PSHOME 'powershell.exe') -NoProfile -File $child -Bootstrap $path -Payload $fixture -Case $case 2>&1)
        $expected = if ($case -eq 'no-distro') { 'Distribution setup declined' } else { 'No setup changes were made' }
        if ($LASTEXITCODE -ne 1 -or (Test-Path (Join-Path $fixture 'unexpected-admin')) -or (($messages -join ' ') -notmatch $expected)) {
            throw ('Full bootstrap refusal path failed (' + $case + '): ' + ($messages -join ' '))
        }
    }
} finally { Remove-Item -Recurse -Force $fixture }
Write-Output 'PASS: PowerShell parse, full setup refusal (no admin action), approval helper, argument preservation, NUL handling and native failures; no real WSL/USB/admin action attempted.'
