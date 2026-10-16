# autostart.ps1 - make Jarvis start by itself when you sign in to Windows (or undo that).
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\autostart.ps1 -Install
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\autostart.ps1 -Remove
# Uses a per-user scheduled task (no admin rights needed). If Windows refuses, it falls back to the Startup folder.
param([switch]$Install, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $PSScriptRoot
$name = 'Jarvis Terminal'
$startCmd = Join-Path $dir 'Start-Jarvis.cmd'
$fallback = Join-Path ([Environment]::GetFolderPath('Startup')) 'Jarvis Terminal.cmd'

if ($Remove) {
  try { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop; Write-Output 'Removed the Jarvis sign-in task.' } catch { }
  if (Test-Path $fallback) { Remove-Item $fallback -Force; Write-Output 'Removed the Jarvis Startup-folder entry.' }
  $marker = Join-Path $dir 'data\autostart-installed'
  if (-not (Test-Path (Split-Path $marker))) { New-Item -ItemType Directory -Path (Split-Path $marker) | Out-Null }
  Set-Content -Path $marker -Value 'removed' -Encoding ASCII   # stops Start-Jarvis.cmd from installing it again
  Write-Output 'Jarvis will no longer start by itself. Run Install-Autostart.cmd to turn it back on.'
  exit 0
}
if (-not $Install) { Write-Output 'Use -Install or -Remove.'; exit 1 }

$user = "$env:USERDOMAIN\$env:USERNAME"
try {
  $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c start "Jarvis" /min "' + $startCmd + '" --autostart') -WorkingDirectory $dir
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  Write-Output 'Jarvis will start by itself every time you sign in to Windows (task "Jarvis Terminal").'
} catch {
  Set-Content -Path $fallback -Value ("@echo off`r`nstart `"Jarvis`" /min `"" + $startCmd + "`" --autostart") -Encoding ASCII
  Write-Output 'Jarvis will start by itself every time you sign in to Windows (Startup folder).'
}
