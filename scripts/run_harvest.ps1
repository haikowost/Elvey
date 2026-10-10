# RETIRED (2026-10). This used to be fired every 2 hours by the Windows task "Elvey LinkedIn harvest",
# which harvested in the old order and ignored the KYC priority list. It now does nothing except say so,
# so an old scheduled task pointing here can't harvest anything.
#
# Remove that task:      powershell -ExecutionPolicy Bypass -File scripts\unschedule_kyc.ps1
# Run the KYC harvest:   powershell -ExecutionPolicy Bypass -File scripts\run_kyc.ps1
# Optional daily run:    powershell -ExecutionPolicy Bypass -File scripts\schedule_kyc.ps1
param([int]$Limit = 8)

$repo = Split-Path -Parent $PSScriptRoot
$log = Join-Path $repo "data\harvest_schedule.log"
New-Item -ItemType Directory -Force -Path (Join-Path $repo 'data') | Out-Null
$msg = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] run_harvest.ps1 is retired - nothing harvested. Remove the old task: scripts\unschedule_kyc.ps1"
$msg | Add-Content -Path $log
Write-Host $msg -ForegroundColor Yellow
exit 0
