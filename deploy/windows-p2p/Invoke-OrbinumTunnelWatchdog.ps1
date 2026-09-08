[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$KeyPath,

    [Parameter(Mandatory = $true)]
    [string]$RemoteUserHost,

    [int]$RemotePort = 30333,
    [int]$LocalPort = 30333,
    [int]$RestartDelaySeconds = 10,
    [string]$SshExe = "$env:WINDIR\System32\OpenSSH\ssh.exe"
)

$ErrorActionPreference = "Stop"

$stateRoot = Join-Path $env:LOCALAPPDATA "OrbinumTunnel"
$logRoot = Join-Path $stateRoot "logs"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

$mainLog = Join-Path $logRoot "tunnel-watchdog.log"
$sshOut  = Join-Path $logRoot "ssh.out.log"
$sshErr  = Join-Path $logRoot "ssh.err.log"

function Write-TunnelLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff zzz"), $Message
    Add-Content -LiteralPath $mainLog -Value $line -Encoding UTF8
}

function Stop-StaleOrbinumTunnel {
    $escapedRemote = [regex]::Escape($RemoteUserHost)
    $escapedPort = [regex]::Escape([string]$RemotePort)

    $stale = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -eq "ssh.exe" -and
            $_.CommandLine -match $escapedPort -and
            $_.CommandLine -match $escapedRemote
        }

    foreach ($proc in $stale) {
        Write-TunnelLog "stale ssh found at watchdog start (PID=$($proc.ProcessId)); stopping it"
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
}

if (-not (Test-Path -LiteralPath $SshExe -PathType Leaf)) {
    throw "ssh.exe not found: $SshExe"
}
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "SSH private key not found: $KeyPath"
}

$sshArgs = @(
    "-i", $KeyPath,
    "-N",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "TCPKeepAlive=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ConnectionAttempts=1",
    "-R", "0.0.0.0:$RemotePort`:127.0.0.1:$LocalPort",
    $RemoteUserHost
)

Write-TunnelLog "watchdog started (PID=$PID)"
Stop-StaleOrbinumTunnel

while ($true) {
    try {
        Set-Content -LiteralPath $sshOut -Value "" -Encoding UTF8
        Set-Content -LiteralPath $sshErr -Value "" -Encoding UTF8

        Write-TunnelLog "starting ssh reverse tunnel :$RemotePort -> 127.0.0.1:$LocalPort"

        & $SshExe @sshArgs 1>>$sshOut 2>>$sshErr
        $exitCode = $LASTEXITCODE

        Write-TunnelLog "ssh exited (code=$exitCode)"

        if (Test-Path -LiteralPath $sshErr) {
            Get-Content -LiteralPath $sshErr -Tail 12 -ErrorAction SilentlyContinue |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
                ForEach-Object { Write-TunnelLog "ssh stderr: $_" }
        }

        if (Test-Path -LiteralPath $sshOut) {
            Get-Content -LiteralPath $sshOut -Tail 6 -ErrorAction SilentlyContinue |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
                ForEach-Object { Write-TunnelLog "ssh stdout: $_" }
        }
    }
    catch {
        Write-TunnelLog "watchdog error: $($_.Exception.GetType().FullName): $($_.Exception.Message)"
    }

    Write-TunnelLog "restarting ssh in $RestartDelaySeconds seconds"
    Start-Sleep -Seconds $RestartDelaySeconds
}
