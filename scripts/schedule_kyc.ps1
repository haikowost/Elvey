# OPT-IN: register ONE daily, off-peak KYC run (Windows Task Scheduler task "Elvey KYC daily").
# It runs scripts\run_kyc.ps1 -Scheduled from THIS repo's own folder (wherever you cloned it), with a
# small batch in priority order (Key tiers -> P1 -> P2 -> P3). The daily cap / pacing / checkpoint stops
# in config.yaml still apply. Also removes the old every-2-hours "Elvey LinkedIn harvest" task.
#
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_kyc.ps1                  # 06:40 daily, batch 10
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_kyc.ps1 -Time 19:10 -BatchLimit 8
#   powershell -ExecutionPolicy Bypass -File scripts\unschedule_kyc.ps1 -All          # stop it again
#
# Runs only while you're logged on (the LinkedIn window needs your desktop, same as a manual run).
param(
    [string]$Time = '06:40',
    [int]$BatchLimit = 10,
    [string]$TaskName = 'Elvey KYC daily'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $repo 'scripts\run_kyc.ps1'
if (-not (Test-Path $runner)) { throw "run_kyc.ps1 not found next to this script ($runner)" }
if ($Time -notmatch '^\d{1,2}:\d{2}$') { throw "-Time must look like 06:40" }

# the old 2-hourly job first - it must not keep running alongside this one
& (Join-Path $PSScriptRoot 'unschedule_kyc.ps1')

$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Scheduled -BatchLimit $BatchLimit"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Elvey KYC: one small LinkedIn harvest batch in priority order, then the people snapshot. Repo: $repo" -Force | Out-Null

$next = (Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime
New-Item -ItemType Directory -Force -Path (Join-Path $repo 'data') | Out-Null
@{ task = $TaskName; time = $Time; batch_limit = $BatchLimit; repo = $repo; registered_at = (Get-Date -Format s);
   next_run = "$next" } | ConvertTo-Json | Set-Content -Path (Join-Path $repo 'data\schedule.json') -Encoding UTF8
Write-Host "Scheduled '$TaskName' daily at $Time (batch $BatchLimit per tier, daily cap still applies). Next run: $next" -ForegroundColor Green
Write-Host "Runs: powershell $arg"
Write-Host "Remove with: powershell -ExecutionPolicy Bypass -File scripts\unschedule_kyc.ps1 -All"
