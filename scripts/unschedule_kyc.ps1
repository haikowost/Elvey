# Removes the OLD every-2-hours Windows scheduled task "Elvey LinkedIn harvest" (it ran run_harvest.ps1
# from whatever folder it was created in - usually an old checkout that ignores the KYC priority order).
# Safe to run any time: a task that isn't there is simply reported as not found.
#
#   powershell -ExecutionPolicy Bypass -File scripts\unschedule_kyc.ps1            # old task only
#   powershell -ExecutionPolicy Bypass -File scripts\unschedule_kyc.ps1 -All       # also the daily "Elvey KYC daily"
param([switch]$All)
$ErrorActionPreference = 'Continue'   # schtasks writes 'not found' to stderr; that's fine here

$tasks = @('Elvey LinkedIn harvest')
if ($All) { $tasks += 'Elvey KYC daily' }

foreach ($name in $tasks) {
    & schtasks /Query /TN "$name" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Not scheduled: '$name' (nothing to remove)."
        continue
    }
    & schtasks /Delete /TN "$name" /F | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "Removed scheduled task '$name'." -ForegroundColor Green }
    else { Write-Host "Couldn't remove '$name' - run this from an elevated PowerShell, or delete it in Task Scheduler." -ForegroundColor Yellow }
}

$repo = Split-Path -Parent $PSScriptRoot
$marker = Join-Path $repo 'data\schedule.json'
if ($All -and (Test-Path $marker)) { Remove-Item $marker -Force }
