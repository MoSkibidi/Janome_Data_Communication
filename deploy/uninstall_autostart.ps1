# Removes the scheduled task created by install_autostart.ps1 and (unless -KeepRunning) stops the panel it started.
# -KeepRunning is used by the panel's own "Turn off" button, so the panel does not stop itself.
param(
    [switch]$KeepRunning
)

$ErrorActionPreference = 'Stop'
$TaskName = 'JanomeJP4Panel'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if (-not $KeepRunning) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Scheduled task '$TaskName' removed."
} else {
    Write-Host "Scheduled task '$TaskName' is not installed."
}
