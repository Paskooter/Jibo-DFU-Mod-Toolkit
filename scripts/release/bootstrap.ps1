[CmdletBinding()]
param([string]$PayloadRoot = $PSScriptRoot, [string]$Distro, [switch]$SelfCheck)
$ErrorActionPreference = 'Stop'

function Confirm-Setup([string]$Question) {
    return ((Read-Host "$Question [y/N]") -match '^(?i:y|yes)$')
}
function Invoke-ApprovedAdmin([string]$File, [string]$Arguments) {
    $p = Start-Process -FilePath $File -ArgumentList $Arguments -Verb RunAs -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Setup exited with code $($p.ExitCode). Restart this app after any requested reboot." }
}
function Native([string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $lines = @(& wsl.exe @Arguments 2>&1); $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $old }
    if ($code -ne 0) { throw "WSL failed: $($lines -join ' ')" }
    return (($lines | ForEach-Object { $_.ToString() -replace "`0", '' }) -join "`n").Trim()
}
function Test-CompatibleDistribution([string]$Name) {
    try {
        $libc = Native -Arguments @('--distribution', $Name, '--exec', 'getconf', 'GNU_LIBC_VERSION')
        if ($libc -notmatch '^glibc\s+(\d+\.\d+)$') { return $false }
        return ([version]$Matches[1] -ge [version]'2.35')
    } catch { return $false }
}
try {
    $archive = Join-Path $PayloadRoot 'payload.tar.gz'
    $usbScript = Join-Path $PayloadRoot 'run.ps1'
    if (-not (Test-Path $archive) -or -not (Test-Path $usbScript)) { throw 'Embedded payload is incomplete.' }
    if ($SelfCheck) { Write-Output 'Bootstrap scripts and Linux payload are present; WSL setup not attempted.'; exit 0 }
    Write-Host 'Jibo DFU Toolkit: Windows uses WSL 2 + WSLg and usbipd-win. Setup requires internet only if these are missing.'
    $ready = $false
    try { $version = Native -Arguments @('--version'); $ready = ($version -match 'WSLg') } catch { $ready = $false }
    if (-not $ready) {
        if (-not (Confirm-Setup 'Install/update WSL 2 and WSLg? Windows will ask for administrator approval.')) { throw 'WSL setup declined. No setup changes were made.' }
        Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--install --no-distribution'
        Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--update'
        throw 'WSL setup requested. Reboot if Windows requests it, then run this download again.'
    }
    $rows = @()
    try { $listing = Native -Arguments @('--list', '--verbose') }
    catch { $listing = ''; Write-Host 'No usable installed WSL distribution was listed; setup can install Ubuntu 22.04.' }
    foreach ($line in ($listing -split "`n")) {
        if ($line -match '^\s*(?<default>\*)?\s*(?<name>.+?)\s+\S+\s+(?<version>[12])\s*$') {
            $rows += [pscustomobject]@{Name=$Matches['name'].Trim(); Version=[int]$Matches['version']; Default=($Matches['default'] -eq '*')}
        }
    }
    $available = @($rows | Where-Object { $_.Version -eq 2 })
    if ($available.Count -eq 0) {
        if (-not (Confirm-Setup 'Install Ubuntu 22.04 for WSL 2? Approve the Windows prompt and create a Linux username/password when asked.')) { throw 'Distribution setup declined.' }
        Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--install --distribution Ubuntu-22.04'
        throw 'Finish Ubuntu first-run username/password setup, then reopen this app.'
    }
    if ($Distro) {
        $selected = @($available | Where-Object { $_.Name -eq $Distro })
        if ($selected.Count -ne 1) { throw 'Requested distribution is not installed as WSL 2.' }
        $Distro = $selected[0].Name
    } else {
        $selected = @($available | Where-Object { $_.Default })
        if ($selected.Count -eq 1) { $Distro = $selected[0].Name }
        elseif ($available.Count -eq 1) { $Distro = $available[0].Name }
        else {
            for ($i=0; $i -lt $available.Count; $i++) { Write-Host "$($i+1). $($available[$i].Name)" }
            $choice = Read-Host 'Choose the WSL 2 distribution number'
            $number = 0
            if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $available.Count) { throw 'Invalid distribution choice.' }
            $Distro = $available[$number-1].Name
        }
    }
    if (-not (Test-CompatibleDistribution $Distro)) {
        Write-Host "The prebuilt payload needs glibc 2.35+ (Ubuntu 22.04+); '$Distro' is incompatible." -ForegroundColor Yellow
        $compatible = @($available | Where-Object { Test-CompatibleDistribution $_.Name })
        if ($compatible.Count -gt 0) {
            for ($i=0; $i -lt $compatible.Count; $i++) { Write-Host "$($i+1). $($compatible[$i].Name)" }
            $choice = Read-Host 'Choose a compatible distribution number, or press Enter to cancel'
            $number = 0
            if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 1 -or $number -gt $compatible.Count) { throw 'Distribution selection cancelled.' }
            $Distro = $compatible[$number-1].Name
        } else {
            if (-not (Confirm-Setup 'Install compatible Ubuntu 22.04? No compiler or toolkit build will be needed.')) { throw 'Compatible distribution setup declined.' }
            Invoke-ApprovedAdmin "$env:SystemRoot\System32\wsl.exe" '--install --distribution Ubuntu-22.04'
            throw 'Finish Ubuntu username/password setup, then reopen this app and choose Ubuntu-22.04.'
        }
    }
    if ($Distro -match '["\r\n]') { throw 'Invalid distribution name.' }
    Native -Arguments @('--distribution', $Distro, '--exec', 'test', '-d', '/mnt/wslg') | Out-Null
    $arch = Native -Arguments @('--distribution', $Distro, '--exec', 'uname', '-m')
    if ($arch -ne 'x86_64') { throw 'This Windows x64 download needs an x86_64 WSL distribution.' }
    $usb = Get-Command usbipd.exe -ErrorAction SilentlyContinue
    if (-not $usb) { $usb = Get-Command usbipd -ErrorAction SilentlyContinue }
    if (-not $usb) {
        if (-not (Confirm-Setup 'Install usbipd-win using the interactive Windows installer?')) { throw 'USB setup declined.' }
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) { throw 'Install Microsoft App Installer (winget) or usbipd-win from https://github.com/dorssel/usbipd-win/releases, then reopen this app.' }
        Invoke-ApprovedAdmin $winget.Source 'install --interactive --exact --id dorssel.usbipd-win'
        throw 'usbipd-win setup finished. Reopen this app so Windows refreshes PATH.'
    }
    $linuxHome = Native -Arguments @('--distribution', $Distro, '--exec', 'printenv', 'HOME')
    if (-not $linuxHome.StartsWith('/') -or $linuxHome.Contains("`n")) { throw 'Invalid WSL home directory.' }
    $revision = (Get-Content (Join-Path $PayloadRoot 'revision.txt') -Raw).Trim()
    if ($revision -notmatch '^[0-9a-f]{40}$') { throw 'Invalid embedded revision.' }
    $target = "$linuxHome/.local/share/jibo-dfu/$revision"
    $linuxArchive = Native -Arguments @('--distribution', $Distro, '--exec', 'wslpath', '-u', '--', $archive)
    Native -Arguments @('--distribution', $Distro, '--exec', 'mkdir', '-p', '--', $target) | Out-Null
    Native -Arguments @('--distribution', $Distro, '--exec', 'tar', '-xzf', $linuxArchive, '-C', $target) | Out-Null
    Native -Arguments @('--distribution', $Distro, '--exec', "$target/jibo-dfu", '--self-check') | Write-Host
    # Exact existing bind/attach/reattach implementation, including validated bus IDs,
    # UAC approval, APX 0955:7740 -> DFU 0955:701a and retry backoff.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $usbScript -UsbOnly -Distro $Distro
    $monitorArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -UsbOnly -MonitorUsb -Distro "{1}"' -f $usbScript, $Distro
    $monitor = Start-Process powershell.exe -ArgumentList $monitorArgs -NoNewWindow -PassThru
    try {
        & wsl.exe --distribution $Distro --exec bash "$target/AppRun"
        $code = $LASTEXITCODE
    } finally {
        if ($monitor -and -not $monitor.HasExited) { Stop-Process -Id $monitor.Id; $monitor.WaitForExit() }
    }
    exit $code
} catch {
    Write-Host "Launcher: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}
