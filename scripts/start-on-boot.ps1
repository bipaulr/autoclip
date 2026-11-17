# Starts autoclip when you log in to Windows. Run ONCE from PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\scripts\start-on-boot.ps1
# It registers a Task Scheduler task "autoclip" for your user (no admin rights needed).
# Remove it later with:  Unregister-ScheduledTask -TaskName autoclip -Confirm:$false
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "scripts\run-autoclip.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "autoclip" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Start Docker Desktop and the autoclip stack at logon" -Force | Out-Null
Write-Host "Registered. autoclip will start each time you log in."
