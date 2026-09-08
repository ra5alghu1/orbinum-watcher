# Windows P2P reverse-tunnel watchdog

This helper keeps the Orbinum validator P2P reverse SSH tunnel alive on a Windows validator host without restarting Docker or the validator itself.

It is intended for the case where the validator remains healthy locally but `ssh.exe` exits and the VPS-side reverse port disappears.

## Behavior

- runs from Windows Task Scheduler at system startup
- starts the reverse SSH tunnel in `BatchMode`
- detects `ssh.exe` exit
- records the SSH exit code and recent stderr/stdout
- waits 10 seconds and starts a new SSH process
- kills only an orphaned SSH process matching the configured remote host and reverse port when the watchdog itself is restarted
- never stops or restarts Docker or the Orbinum validator

Logs are written to:

```text
%LOCALAPPDATA%\OrbinumTunnel\logs\tunnel-watchdog.log
%LOCALAPPDATA%\OrbinumTunnel\logs\ssh.err.log
%LOCALAPPDATA%\OrbinumTunnel\logs\ssh.out.log
```

## Install

Copy the watchdog to:

```text
C:\ProgramData\Orbinum\Invoke-OrbinumTunnelWatchdog.ps1
```

Then run PowerShell as Administrator:

```powershell
.\Install-OrbinumTunnelWatchdog.ps1 `
  -KeyPath "C:\Users\YOUR_USER\.ssh\orbinum_tunnel" `
  -RemoteUserHost "orbinum-tunnel@YOUR_VPS_IP" `
  -RemotePort 30333 `
  -LocalPort 30333
```

The installer asks for the Windows account password because the Scheduled Task is configured to run at system startup even before an interactive sign-in.

## Verify

```powershell
Get-ScheduledTask -TaskName "Orbinum SSH Tunnel" |
  Select-Object TaskName, State

Get-Content "$env:LOCALAPPDATA\OrbinumTunnel\logs\tunnel-watchdog.log" -Tail 20

Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -eq "ssh.exe" -and
    $_.CommandLine -match "30333"
  } |
  Select-Object ProcessId, CreationDate, CommandLine |
  Format-List
```

On the VPS, confirm the reverse listener:

```bash
ss -lntp | grep 30333
```

## Recovery test

To test only tunnel recovery, first identify the matching `ssh.exe` PID, then terminate only that process:

```powershell
Stop-Process -Id <SSH_PID> -Force
```

Expected watchdog sequence:

```text
ssh exited (code=...)
restarting ssh in 10 seconds
starting ssh reverse tunnel :30333 -> 127.0.0.1:30333
```

A new matching `ssh.exe` process should appear and the VPS listener should return. The validator container should remain untouched throughout the test.
