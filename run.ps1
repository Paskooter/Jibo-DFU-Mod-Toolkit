[CmdletBinding()]
param(
    [switch]$ManualUsb,
    [string]$Distro,
    [switch]$UsbOnly,
    [switch]$MonitorUsb
)

$ErrorActionPreference = 'Stop'
$JiboVendorId = '0955'
$JiboProductIds = @('7740', '701a')
$RepoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$UsbipdPath = $null
$ElevationDeclined = $false
$ElevationFailures = @{}
$AttachFailures = @{}
$StateWarningShown = $false

function Invoke-CapturedNativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    # Windows PowerShell 5.1 can promote native stderr to a terminating
    # NativeCommandError when the caller uses Stop. Keep stderr captured as
    # text so nonzero exit codes can be handled as normal launcher failures.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = @()
    $exitCode = 1
    $startError = $null
    try {
        $output = @(& $FilePath @ArgumentList 2>&1)
        $exitCode = $LASTEXITCODE
    }
    catch {
        $startError = $_.Exception.Message
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    [pscustomobject]@{
        Output = $output
        ExitCode = $exitCode
        StartError = $startError
    }
}

function Write-ManualUsbInstructions {
    param(
        [object[]]$Devices = @(),
        [string]$Reason = ''
    )

    if ($Reason) {
        Write-Host $Reason -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host 'Manual USB handoff:' -ForegroundColor Cyan
    if ($Devices.Count -gt 0) {
        foreach ($device in $Devices) {
            $pidText = $device.ProductId
            Write-Host ("  Jibo {0}:{1} on BUSID {2} ({3})" -f $JiboVendorId, $pidText, $device.BusId, $device.Description)
            if (-not $device.IsBound) {
                Write-Host ("    In an Administrator PowerShell window, run: usbipd bind --busid {0}" -f $device.BusId)
            }
            Write-Host ("    In a normal PowerShell window, run: usbipd attach --wsl `"{0}`" --busid {1}" -f $script:SelectedDistro, $device.BusId)
        }
    }
    else {
        Write-Host '  Run `usbipd list` to find the connected Jibo entry (VID:PID 0955:7740 or 0955:701a) and its BUSID.'
        Write-Host '  In an Administrator PowerShell window, run `usbipd bind --busid <BUSID>` if it is not already shared.'
        Write-Host ("  In a normal PowerShell window, run `usbipd attach --wsl `"{0}`" --busid <BUSID>`." -f $script:SelectedDistro)
    }
    Write-Host '  Binding is a one-time Windows administrator action. Reattach after the robot changes USB mode or WSL restarts.'
    Write-Host ''
}

function Get-Wsl2Distributions {
    $result = Invoke-CapturedNativeCommand -FilePath $script:WslPath -ArgumentList @('--list', '--verbose')
    if ($result.ExitCode -ne 0) {
        $detail = (($result.Output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine)
        if ($result.StartError) { $detail = $result.StartError }
        throw "Could not list installed WSL distributions. $detail"
    }

    $rows = @()
    foreach ($lineObject in $result.Output) {
        $line = ($lineObject.ToString() -replace "`0", '').TrimEnd()
        if ($line -match '^\s*(?<default>\*)?\s*(?<name>.+?)\s+\S+\s+(?<version>[12])\s*$') {
            $rows += [pscustomobject]@{
                Name = $Matches['name'].Trim()
                Version = [int]$Matches['version']
                IsDefault = ($Matches['default'] -eq '*')
            }
        }
    }
    return $rows
}

function Get-UsbipdState {
    if (-not $script:UsbipdPath) {
        return $null
    }
    try {
        $result = Invoke-CapturedNativeCommand -FilePath $script:UsbipdPath -ArgumentList @('state')
        if ($result.ExitCode -ne 0) {
            $detail = ($result.Output | ForEach-Object { $_.ToString() }) -join ' '
            if ($result.StartError) { $detail = $result.StartError }
            throw "usbipd state exited with code $($result.ExitCode). $detail"
        }
        $json = ($result.Output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        return ($json | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        if (-not $script:StateWarningShown) {
            Write-Host ("Could not read structured usbipd state: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
            Write-ManualUsbInstructions -Reason 'Automatic USB detection is unavailable.'
            $script:StateWarningShown = $true
        }
        return $null
    }
}

function Get-JiboUsbDevices {
    $state = Get-UsbipdState
    if (-not $state -or -not $state.Devices) {
        return @()
    }

    $devices = @()
    foreach ($entry in $state.Devices) {
        if (-not $entry.BusId -or -not $entry.InstanceId) {
            continue
        }
        if ($entry.InstanceId -match '(?i)VID_0955&PID_([0-9a-f]{4})(?=&|\\|$)') {
            $productId = $Matches[1].ToLowerInvariant()
            if ($script:JiboProductIds -contains $productId) {
                $devices += [pscustomobject]@{
                    BusId = [string]$entry.BusId
                    InstanceId = [string]$entry.InstanceId
                    Description = [string]$entry.Description
                    ProductId = $productId
                    IsBound = ($null -ne $entry.PersistedGuid)
                    IsAttached = ($null -ne $entry.ClientIPAddress)
                }
            }
        }
    }
    return $devices
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-UacBind {
    param([string]$BusId)

    if ($BusId -notmatch '^\d+-\d+(?:\.\d+)*$') {
        Write-Host ("Refusing to bind an invalid USB bus ID: {0}" -f $BusId) -ForegroundColor Yellow
        return $false
    }
    if ($script:ElevationDeclined) {
        return $false
    }
    $isAdministrator = Test-IsAdministrator
    if ($isAdministrator) {
        Write-Host ("Sharing Jibo on USB port {0}." -f $BusId) -ForegroundColor Cyan
        $result = Invoke-CapturedNativeCommand -FilePath $script:UsbipdPath -ArgumentList @('bind', '--busid', $BusId)
        if ($result.ExitCode -ne 0) {
            $detail = ($result.Output | ForEach-Object { $_.ToString() }) -join ' '
            if ($result.StartError) { $detail = $result.StartError }
            Write-Host ("usbipd bind failed with exit code {0}: {1}" -f $result.ExitCode, $detail) -ForegroundColor Yellow
            return $false
        }
        return $true
    }

    Write-Host ("Sharing Jibo on USB port {0}; Windows may ask for administrator approval." -f $BusId) -ForegroundColor Cyan
    try {
        # BusId was validated above to keep it safe as a Start-Process argument.
        $argumentLine = 'bind --busid ' + $BusId
        $process = Start-Process -FilePath $script:UsbipdPath -ArgumentList $argumentLine -Verb RunAs -Wait -PassThru -WorkingDirectory $env:SystemRoot
        if ($process.ExitCode -ne 0) {
            Write-Host ("Elevated usbipd bind failed with exit code {0}." -f $process.ExitCode) -ForegroundColor Yellow
            return $false
        }
        return $true
    }
    catch {
        $script:ElevationDeclined = $true
        Write-ManualUsbInstructions -Reason 'Administrator approval was not granted, so the launcher will leave USB sharing to you.'
        return $false
    }
}

function Invoke-JiboAttach {
    param([object]$Device)

    if ([string]$Device.BusId -notmatch '^\d+-\d+(?:\.\d+)*$') {
        Write-Host ("Refusing to attach an invalid USB bus ID: {0}" -f $Device.BusId) -ForegroundColor Yellow
        return $false
    }
    if ($Device.IsAttached) {
        return $true
    }
    $key = $Device.InstanceId
    $now = [DateTime]::UtcNow
    if ($script:AttachFailures.ContainsKey($key) -and ($now - $script:AttachFailures[$key]).TotalSeconds -lt 8) {
        return $false
    }
    if (-not $Device.IsBound) {
        if ($script:ElevationFailures.ContainsKey($key) -and ($now - $script:ElevationFailures[$key]).TotalSeconds -lt 30) {
            return $false
        }
        if (-not (Invoke-UacBind -BusId $Device.BusId)) {
            $script:ElevationFailures[$key] = $now
            return $false
        }

        # Binding can take a moment to appear in usbipd's state snapshot.
        Start-Sleep -Milliseconds 400
        $refreshed = @(Get-JiboUsbDevices | Where-Object { $_.BusId -eq $Device.BusId })
        if ($refreshed.Count -gt 0) {
            $Device = $refreshed[0]
        }
        if (-not $Device.IsBound) {
            $script:ElevationFailures[$key] = [DateTime]::UtcNow
            Write-ManualUsbInstructions -Devices @($Device) -Reason 'The device is still not shared after the bind attempt.'
            return $false
        }
    }

    Write-Host ("Attaching Jibo {0}:{1} on USB port {2} to WSL distro {3}." -f $JiboVendorId, $Device.ProductId, $Device.BusId, $script:SelectedDistro) -ForegroundColor Cyan
    $result = Invoke-CapturedNativeCommand -FilePath $script:UsbipdPath -ArgumentList @('attach', '--wsl', $script:SelectedDistro, '--busid', $Device.BusId)
    if ($result.ExitCode -eq 0) {
        $script:AttachFailures.Remove($key)
        return $true
    }

    $script:AttachFailures[$key] = [DateTime]::UtcNow
    $detail = ($result.Output | ForEach-Object { $_.ToString() }) -join ' '
    if ($result.StartError) { $detail = $result.StartError }
    Write-Host ("usbipd attach failed: {0}" -f $detail) -ForegroundColor Yellow
    Write-ManualUsbInstructions -Devices @($Device) -Reason 'The launcher will keep watching for this Jibo USB state.'
    return $false
}

function Invoke-JiboUsbCycle {
    $devices = @(Get-JiboUsbDevices)
    if ($script:StateWarningShown) {
        return [pscustomobject]@{ StateUnavailable = $true; FoundDevice = ($devices.Count -gt 0); AttachFailed = $false }
    }

    $attachFailed = $false
    foreach ($device in $devices) {
        if (-not (Invoke-JiboAttach -Device $device)) {
            $attachFailed = $true
        }
    }
    return [pscustomobject]@{
        StateUnavailable = $false
        FoundDevice = ($devices.Count -gt 0)
        AttachFailed = $attachFailed
    }
}

function Get-LinuxRepoPath {
    param([string]$DistroName)

    $unc = [regex]::Match($script:RepoRoot, '^(?:\\\\wsl\.localhost|\\\\wsl\$)\\(?<distro>[^\\]+)(?<path>\\.*)?$', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if ($unc.Success) {
        $sourceDistro = $unc.Groups['distro'].Value
        if ($sourceDistro -ine $DistroName) {
            throw ("The repository is inside WSL distro '{0}'. Run it with -Distro '{0}' so WSL can access the files." -f $sourceDistro)
        }
        $linuxPath = $unc.Groups['path'].Value.Replace('\', '/')
        if (-not $linuxPath) {
            $linuxPath = '/'
        }
        return $linuxPath
    }

    $result = Invoke-CapturedNativeCommand -FilePath $script:WslPath -ArgumentList @('--distribution', $DistroName, '--exec', 'wslpath', '-u', '--', $script:RepoRoot)
    if ($result.ExitCode -ne 0) {
        $detail = ($result.Output | ForEach-Object { $_.ToString() }) -join ' '
        if ($result.StartError) { $detail = $result.StartError }
        throw "Could not convert the repository path for WSL. $detail"
    }
    $linuxPath = (($result.Output | ForEach-Object { $_.ToString() }) -join '').Trim()
    if (-not $linuxPath.StartsWith('/')) {
        throw ("WSL returned an invalid repository path: {0}" -f $linuxPath)
    }
    return $linuxPath
}

function Select-Distribution {
    param([object[]]$Distributions, [string]$Requested)

    $available = @($Distributions | Where-Object { $_.Version -eq 2 })
    if ($available.Count -eq 0) {
        throw 'No installed WSL 2 distribution was found. This launcher does not install WSL or a Linux distribution.'
    }
    if ($Requested) {
        $selected = @($available | Where-Object { $_.Name -ieq $Requested })
        if ($selected.Count -ne 1) {
            $names = ($available | ForEach-Object { $_.Name }) -join ', '
            throw ("'{0}' is not an installed WSL 2 distro. Available WSL 2 distros: {1}" -f $Requested, $names)
        }
        return $selected[0].Name
    }
    if ($available.Count -eq 1) {
        return $available[0].Name
    }
    $default = @($available | Where-Object { $_.IsDefault })
    if ($default.Count -eq 1) {
        return $default[0].Name
    }

    Write-Host 'Choose an installed WSL 2 distribution:' -ForegroundColor Cyan
    for ($index = 0; $index -lt $available.Count; $index++) {
        Write-Host ("  {0}. {1}" -f ($index + 1), $available[$index].Name)
    }
    while ($true) {
        $choice = Read-Host 'Enter a number'
        $number = 0
        if ([int]::TryParse($choice, [ref]$number) -and $number -ge 1 -and $number -le $available.Count) {
            return $available[$number - 1].Name
        }
        Write-Host 'Enter one of the listed numbers.' -ForegroundColor Yellow
    }
}

try {
    if ($MonitorUsb -and -not $UsbOnly) {
        throw '-MonitorUsb requires -UsbOnly.'
    }
    if ($UsbOnly -and $ManualUsb) {
        throw '-UsbOnly cannot be combined with -ManualUsb.'
    }

    $script:WslPath = (Get-Command 'wsl.exe' -ErrorAction Stop).Source
    $distributions = @(Get-Wsl2Distributions)
    $script:SelectedDistro = Select-Distribution -Distributions $distributions -Requested $Distro

    if (-not $ManualUsb) {
        $usbipdCommand = Get-Command 'usbipd.exe' -ErrorAction SilentlyContinue
        if (-not $usbipdCommand) {
            $usbipdCommand = Get-Command 'usbipd' -ErrorAction SilentlyContinue
        }
        if ($usbipdCommand) {
            $script:UsbipdPath = $usbipdCommand.Source
        }
        else {
            $reason = 'usbipd-win is not installed or is not on PATH; automatic USB attachment is unavailable.'
            Write-ManualUsbInstructions -Reason $reason
            if (-not $UsbOnly) {
                Write-Host 'If needed, install usbipd-win from an Administrator PowerShell with:'
                Write-Host '  winget install --interactive --exact dorssel.usbipd-win'
                Write-Host ''
            }
        }
    }

    if ($UsbOnly) {
        if (-not $script:UsbipdPath) {
            exit 2
        }

        do {
            $cycle = Invoke-JiboUsbCycle
            if ($cycle.StateUnavailable) {
                exit 2
            }
            if ($cycle.AttachFailed -and -not $MonitorUsb) {
                exit 3
            }
            if (-not $MonitorUsb) {
                if (-not $cycle.FoundDevice) {
                    Write-Host ("No Jibo device (0955:7740 or 0955:701a) is currently connected for WSL distro '{0}'." -f $script:SelectedDistro) -ForegroundColor Yellow
                }
                exit 0
            }
            Start-Sleep -Milliseconds 1200
        } while ($true)
    }

    $linuxRepo = Get-LinuxRepoPath -DistroName $script:SelectedDistro
    $check = Invoke-CapturedNativeCommand -FilePath $script:WslPath -ArgumentList @('--distribution', $script:SelectedDistro, '--exec', 'test', '-f', "$linuxRepo/run.sh")
    if ($check.ExitCode -ne 0) {
        throw ("Could not find run.sh in WSL at {0}. Keep the repository in this distro or launch the script from its matching \\wsl.localhost share." -f $linuxRepo)
    }

    if ($ManualUsb) {
        $script:UsbipdPath = (Get-Command 'usbipd.exe' -ErrorAction SilentlyContinue).Source
        if (-not $script:UsbipdPath) {
            $script:UsbipdPath = (Get-Command 'usbipd' -ErrorAction SilentlyContinue).Source
        }
        $manualDevices = @()
        if ($script:UsbipdPath) {
            $manualDevices = @(Get-JiboUsbDevices)
        }
        Write-ManualUsbInstructions -Devices $manualDevices -Reason 'Manual USB mode is enabled; the launcher will not bind or attach USB devices.'
    }

    if (-not $ManualUsb -and $script:UsbipdPath) {
        # Start the selected WSL2 VM before handing USB over so the menu sees a
        # robot that was already connected when the launcher started.
        $warmup = Invoke-CapturedNativeCommand -FilePath $script:WslPath -ArgumentList @('--distribution', $script:SelectedDistro, '--exec', 'true')
        if ($warmup.ExitCode -ne 0) {
            $detail = ($warmup.Output | ForEach-Object { $_.ToString() }) -join ' '
            if ($warmup.StartError) { $detail = $warmup.StartError }
            Write-Host ("Could not warm up WSL before USB attachment: {0}" -f $detail) -ForegroundColor Yellow
        }
        $cycle = Invoke-JiboUsbCycle
        if ($cycle.StateUnavailable) {
            Write-Host 'The launcher will continue without automatic USB detection.' -ForegroundColor Yellow
        }
    }

    Write-Host ("Starting Jibo DFU toolkit in WSL distro '{0}'." -f $script:SelectedDistro) -ForegroundColor Green
    $monitor = $null
    if (-not $ManualUsb -and $script:UsbipdPath -and -not $script:StateWarningShown) {
        # Keep USB reattachment running while WSL owns the interactive console.
        # The child uses -File; no encoded PowerShell command is needed.
        $monitorArguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -UsbOnly -MonitorUsb -Distro "{1}"' -f $PSCommandPath, $script:SelectedDistro
        $monitor = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') `
            -ArgumentList $monitorArguments -NoNewWindow -PassThru
    }
    try {
        # Invoke WSL directly so PowerShell passes the distro and Linux path as
        # separate arguments, including when either contains spaces.
        & $script:WslPath --distribution $script:SelectedDistro --cd $linuxRepo --exec env JIBO_MANUAL_USB=1 bash ./run.sh
        $wslExitCode = $LASTEXITCODE
    }
    finally {
        if ($monitor -and -not $monitor.HasExited) {
            Stop-Process -Id $monitor.Id
            $monitor.WaitForExit()
        }
    }
    exit $wslExitCode
}
catch {
    Write-Host ("Launcher error: {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
