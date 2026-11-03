# Installs a Windows scheduled task that starts the Janome JP4 panel automatically and restarts it
# if it exits. After a power cut the panel comes back by itself and, if a run was active (or
# "start automatically" is ticked in Settings), the reader resumes without anyone pressing Start.
#
#   Startup (default)  runs at boot as SYSTEM, before anyone logs in. Needs an Administrator PowerShell.
#   Logon              runs when the current user logs in. No administrator rights needed.
#
# Usage:  powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1 [-Mode Startup|Logon]
# Remove: powershell -ExecutionPolicy Bypass -File deploy\uninstall_autostart.ps1

param(
    [ValidateSet('Startup', 'Logon')]
    [string]$Mode = 'Startup'
)

$ErrorActionPreference = 'Stop'
$TaskName = 'JanomeJP4Panel'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $ProjectDir 'run_service.bat'
if (-not (Test-Path $Runner)) { throw "run_service.bat not found next to the deploy folder: $Runner" }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Mode -eq 'Startup' -and -not $isAdmin) {
    throw "Startup mode needs an Administrator PowerShell. Run it as administrator, or use -Mode Logon."
}

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$Runner`"" -WorkingDirectory $ProjectDir
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew

if ($Mode -eq 'Startup') {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = 'PT30S'   # let the USB-serial driver and the network come up first
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Scheduled task '$TaskName' installed ($Mode). It runs: $Runner"
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Panel log: $(Join-Path $ProjectDir 'logs\panel.log')"
