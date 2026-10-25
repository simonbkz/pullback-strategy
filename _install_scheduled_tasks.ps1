# Install scheduled tasks to auto-start live_trader + watchdog on boot.
# Matches the pattern of existing fx-edge-cf-pipeline / fx-edge-monitor tasks
# (UserId=Administrator, LogonType=Interactive, RunLevel=Limited).

$root = "c:\Users\Administrator\fx-edge"
$venvPy = "$root\.venv\Scripts\python.exe"

# Common principal + settings matching existing fx-edge-* tasks
$principal = New-ScheduledTaskPrincipal `
    -UserId "Administrator" `
    -LogonType Interactive `
    -RunLevel Limited

# Live-trader task: no execution time limit (runs forever)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -RestartCount 10

# ---- Task 1: fx-edge-live-trader ----
$name1 = "fx-edge-live-trader"
if (Get-ScheduledTask -TaskName $name1 -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name1 -Confirm:$false
    Write-Output "removed existing task: $name1"
}
$action1 = New-ScheduledTaskAction `
    -Execute $venvPy `
    -Argument "-u live_trader.py" `
    -WorkingDirectory $root
$trigger1 = New-ScheduledTaskTrigger -AtStartup
$trigger1.Delay = "PT60S"
Register-ScheduledTask `
    -TaskName $name1 `
    -Action $action1 `
    -Trigger $trigger1 `
    -Principal $principal `
    -Settings $settings `
    -Description "fx-edge live trader (auto-start on boot, 60s delay). Trades MT5 pullback-trend strategy." | Out-Null
Write-Output "registered: $name1 (AtStartup + 60s)"

# ---- Task 2: fx-edge-watchdog ----
$name2 = "fx-edge-watchdog"
if (Get-ScheduledTask -TaskName $name2 -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name2 -Confirm:$false
    Write-Output "removed existing task: $name2"
}
$action2 = New-ScheduledTaskAction `
    -Execute $venvPy `
    -Argument "watchdog.py --auto-restart" `
    -WorkingDirectory $root
$trigger2 = New-ScheduledTaskTrigger -AtStartup
$trigger2.Delay = "PT120S"
Register-ScheduledTask `
    -TaskName $name2 `
    -Action $action2 `
    -Trigger $trigger2 `
    -Principal $principal `
    -Settings $settings `
    -Description "fx-edge watchdog (auto-start on boot, 120s delay). Monitors live_trader.py and auto-relaunches if missing." | Out-Null
Write-Output "registered: $name2 (AtStartup + 120s)"

Write-Output ""
Write-Output "=== registered fx-edge scheduled tasks ==="
Get-ScheduledTask | Where-Object { $_.TaskName -like "fx-edge*" } |
    Select-Object TaskName, State, @{n='Trigger';e={($_.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ','}} |
    Format-Table -AutoSize
