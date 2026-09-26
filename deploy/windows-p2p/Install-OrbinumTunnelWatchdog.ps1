#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$KeyPath,

    [Parameter(Mandatory = $true)]
    [string]$RemoteUserHost,

    [int]$RemotePort = 30333,
    [int]$LocalPort = 30333,
    [string]$TaskName = "Orbinum SSH Tunnel",
    [string]$WatchdogPath = "C:\ProgramData\Orbinum\Invoke-OrbinumTunnelWatchdog.ps1"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $WatchdogPath -PathType Leaf)) {
    throw "Watchdog script not found: $WatchdogPath"
}
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "SSH private key not found: $KeyPath"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Host "This updates ONLY '$TaskName'. Docker and the Orbinum validator are not touched."
Write-Host "Enter the Windows account password for $identity (not the PIN)."
$credential = Get-Credential -UserName $identity -Message "Orbinum SSH tunnel startup task"
if ($null -eq $credential) {
    throw "Windows credentials are required."
}

$password = $credential.GetNetworkCredential().Password
if ([string]::IsNullOrWhiteSpace($password)) {
    throw "The Windows account password cannot be empty."
}

$actionArgs = @(
    "-NoProfile"
    "-NonInteractive"
    "-WindowStyle Hidden"
    "-ExecutionPolicy Bypass"
    "-File `"$WatchdogPath`""
    "-KeyPath `"$KeyPath`""
    "-RemoteUserHost `"$RemoteUserHost`""
    "-RemotePort $RemotePort"
    "-LocalPort $LocalPort"
) -join " "

$action = New-ScheduledTaskAction `
    -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument $actionArgs

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    $escapedRemote = [regex]::Escape($RemoteUserHost)
    $escapedPort = [regex]::Escape([string]$RemotePort)

    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -eq "ssh.exe" -and
            $_.CommandLine -match $escapedPort -and
            $_.CommandLine -match $escapedRemote
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -User $identity `
        -Password $password `
        -RunLevel Highest `
        -Settings $settings `
        -Description "Self-healing reverse SSH tunnel for Orbinum P2P port $RemotePort" `
        -Force | Out-Null
}
finally {
    $password = $null
    $credential = $null
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Host ""
Write-Host "TaskName       : $($task.TaskName)"
Write-Host "State          : $($task.State)"
Write-Host "LastTaskResult : $($info.LastTaskResult)"
Write-Host "Watchdog       : $WatchdogPath"
Write-Host "Log            : $env:LOCALAPPDATA\OrbinumTunnel\logs\tunnel-watchdog.log"
