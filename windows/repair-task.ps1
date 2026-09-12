$ErrorActionPreference = 'Stop'
$Root = Join-Path $env:ProgramData 'AImanVisualCopilot'
$InstalledScript = Join-Path $Root 'observer.ps1'
$LauncherPath = Join-Path $Root 'observer-launcher.vbs'
$TaskName = 'AImanVisualCopilot Observer'

if (-not (Test-Path $InstalledScript)) { throw "observer.ps1 missing: $InstalledScript" }

$old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($old) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
}
Start-Sleep -Milliseconds 500

# Terminate only old AVC observer PowerShell instances, never unrelated shells.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.ProcessId -ne $PID -and $_.Name -in @('powershell.exe','pwsh.exe') -and
        $_.CommandLine -and $_.CommandLine -like "*$InstalledScript*"
    } |
    ForEach-Object {
        try { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null } catch {}
    }

$vbs = @'
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\ProgramData\AImanVisualCopilot\observer.ps1"""
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
'@
Set-Content -Path $LauncherPath -Value $vbs -Encoding ASCII

$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$action = New-ScheduledTaskAction -Execute $wscript -Argument "//B //NoLogo `"$LauncherPath`""
$userId = if ($old -and $old.Principal.UserId) { $old.Principal.UserId } else { "$env:USERDOMAIN\$env:USERNAME" }
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 3650) -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
try { $settings.Hidden = $true } catch {}
if ($old -and $old.Principal) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $old.Principal -Description 'Read-only Windows observer for AImanVisualCopilot' -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Read-only Windows observer for AImanVisualCopilot' -Force | Out-Null
}
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$t = Get-ScheduledTask -TaskName $TaskName
$p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine -like '*AImanVisualCopilot*observer.ps1*' } | Select-Object ProcessId,Name,CommandLine
[pscustomobject]@{
  ok = $true
  task_state = [string]$t.State
  task_hidden = [bool]$t.Settings.Hidden
  action = [string]$t.Actions.Execute
  arguments = [string]$t.Actions.Arguments
  principal = [string]$t.Principal.UserId
  logon_type = [string]$t.Principal.LogonType
  observer_processes = @($p)
} | ConvertTo-Json -Depth 5 -Compress
