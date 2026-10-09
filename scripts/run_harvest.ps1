# Runs one harvest batch and appends the output to data\harvest_schedule.log.
# Meant to be called by a Windows Scheduled Task every couple of hours during the day,
# instead of one long manual run -- see "Scheduling the harvester" in README.md.
#
# Usage (from Task Scheduler or a terminal): powershell -ExecutionPolicy Bypass -File scripts\run_harvest.ps1 [-Limit 8]
param([int]$Limit = 8)

$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$log = Join-Path $repo "data\harvest_schedule.log"
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

"`n[$stamp] starting (limit=$Limit)" | Add-Content -Path $log
$py = Join-Path $repo ".venv\Scripts\python.exe"   # the venv scripts\run_kyc.ps1 sets up, else PATH python
if (-not (Test-Path $py)) { $py = "python" }
& $py -m src.harvest --limit $Limit *>> $log
"[$stamp] exit code $LASTEXITCODE" | Add-Content -Path $log
