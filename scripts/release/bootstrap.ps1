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
function ConvertTo-NativeArgumentLine([string[]]$Arguments) {
    # Start-Process joins arrays on Windows. Quote for the native argv parser,
    # not PowerShell; preserve spaces, embedded quotes and trailing backslashes.
    return (($Arguments | ForEach-Object {
        '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
}
function Start-WslUsbAccess([string]$Name, [string]$User, [string]$Uid, [string]$Gid, [string]$Helper, [string]$Session) {
    if ($Uid -notmatch '^[1-9][0-9]*$' -or $Gid -notmatch '^[0-9]+$') { throw 'USB access requires a non-root Linux user.' }
    $wsl = "$env:SystemRoot\System32\wsl.exe"
    $rootArgs = @('--distribution', $Name, '--user', 'root', '--exec', 'bash', $Helper, $Uid, $Gid, 'once')
    Write-Host 'Windows will ask for administrator approval for Jibo USB access. The GUI will remain your normal Linux user.'
    Invoke-ApprovedAdmin $wsl (ConvertTo-NativeArgumentLine $rootArgs)
    Native -Arguments @('--distribution', $Name, '--user', $User, '--exec', 'touch', '--', $Session) | Out-Null
    $rootArgs[-1] = $Session
    return (Start-Process -FilePath $wsl -ArgumentList (ConvertTo-NativeArgumentLine $rootArgs) -NoNewWindow -PassThru)
}
function Invoke-WslGui([string]$Name, [string]$User, [string]$Target, [string]$Session) {
    # Never enter AppRun's Linux sudo/polkit branch from the Windows EXE.
    & wsl.exe --distribution $Name --user $User --exec bash "$Target/wsl-user-launch.sh" "$Target/jibo-dfu" $Session | Out-Host
    return $LASTEXITCODE
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
    $linuxUser = Native -Arguments @('--distribution', $Distro, '--exec', 'id', '-un')
    $linuxUid = Native -Arguments @('--distribution', $Distro, '--exec', 'id', '-u')
    $linuxGid = Native -Arguments @('--distribution', $Distro, '--exec', 'id', '-g')
    if ($linuxUid -notmatch '^[1-9][0-9]*$' -or $linuxGid -notmatch '^[0-9]+$' -or $linuxUser -match '["\r\n]' -or -not $linuxUser) {
        throw 'Choose a WSL distribution with a non-root default user. The GUI will not run as root.'
    }
    $linuxHome = Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'printenv', 'HOME')
    if (-not $linuxHome.StartsWith('/') -or $linuxHome -match '[\r\n]') { throw 'Invalid WSL home directory.' }
    $revision = (Get-Content (Join-Path $PayloadRoot 'revision.txt') -Raw).Trim()
    if ($revision -notmatch '^[0-9a-f]{40}$') { throw 'Invalid embedded revision.' }
    $target = "$linuxHome/.local/share/jibo-dfu/$revision"
    $linuxArchive = Native -Arguments @('--distribution', $Distro, '--exec', 'wslpath', '-u', '--', $archive)
    Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'mkdir', '-p', '--', $target) | Out-Null
    Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'tar', '-xzf', $linuxArchive, '-C', $target) | Out-Null
    foreach ($helper in @('wsl-usb-permissions.sh', 'wsl-user-launch.sh')) {
        $source = Join-Path $PayloadRoot $helper
        if (-not (Test-Path $source)) { throw 'Embedded Windows USB helper is missing.' }
        $linuxSource = Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'wslpath', '-u', '--', $source)
        Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'cp', '--', $linuxSource, "$target/$helper") | Out-Null
    }
    Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', "$target/jibo-dfu", '--self-check') | Write-Host
    $session = "$target/.usb-$([guid]::NewGuid().ToString('N'))"
    $permissions = $null
    $monitor = $null
    try {
        # Existing validated bind/attach/reattach logic retains usbipd's UAC prompt.
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $usbScript -UsbOnly -Distro $Distro
        if ($LASTEXITCODE -ne 0) { throw 'USB handoff failed or administrator approval was declined. No GUI was started.' }
        $permissions = Start-WslUsbAccess -Name $Distro -User $linuxUser -Uid $linuxUid -Gid $linuxGid -Helper "$target/wsl-usb-permissions.sh" -Session $session
        if ($permissions.WaitForExit(200)) { throw 'The WSL USB permission watcher failed to start.' }
        $monitorArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -UsbOnly -MonitorUsb -Distro "{1}"' -f $usbScript, $Distro
        $monitor = Start-Process powershell.exe -ArgumentList $monitorArgs -NoNewWindow -PassThru
        $code = Invoke-WslGui -Name $Distro -User $linuxUser -Target $target -Session $session
    } finally {
        # Removing the user-owned session makes the root watcher exit itself.
        try { Native -Arguments @('--distribution', $Distro, '--user', $linuxUser, '--exec', 'rm', '-f', '--', $session) | Out-Null }
        catch { Write-Warning 'Could not remove the USB session; the watcher also checks the GUI process lifetime.' }
        if ($permissions -and -not $permissions.HasExited) {
            if (-not $permissions.WaitForExit(2000)) { Stop-Process -Id $permissions.Id; $permissions.WaitForExit() }
        }
        if ($monitor -and -not $monitor.HasExited) { Stop-Process -Id $monitor.Id; $monitor.WaitForExit() }
    }
    exit $code
} catch {
    Write-Host "Launcher: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host 'Press Enter to close' | Out-Null
    exit 1
}
