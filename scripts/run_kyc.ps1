# Elvey KYC - the one command: set up, ingest, harvest LinkedIn (priority order), export, open the graph.
#
#   Right-click > "Run with PowerShell", or from the repo folder:
#     powershell -ExecutionPolicy Bypass -File scripts\run_kyc.ps1
#
# What it does, in order (every step is safe to re-run; the harvester resumes where it stopped):
#   1. Creates .venv if missing and installs requirements.txt (again only when that file changes),
#      plus Playwright's Chromium unless config.yaml sets harvest.browser_channel.
#   2. Ingests the workbooks named in config.yaml: inputs.relational_workbook first (accounts,
#      contacts, top-50/top-500 ranks, sellout, quotes, open Zoho deals), then inputs.workbook (the
#      CLEANED contacts sheet). A dated name that no longer exists falls back to the newest export.
#   3. Matches the KYC face library to contacts.
#   4. Runs the LinkedIn harvester in priority batches: contacts at the top-50 accounts, then the
#      top-500 contacts, then category A. The daily cap / pacing / checkpoint stops in config.yaml
#      apply unchanged. FIRST RUN: a browser window opens - log into LinkedIn there, complete any
#      security check, then press Enter in THIS window. That login is remembered afterwards.
#   5. Exports data\exports\Elvey KYC enriched contacts <date>.xlsx + data\exports\faces\.
#   6. Starts the dashboard (its own window) and opens http://127.0.0.1:<port>/graph/.
#
# Switches: -SkipHarvest (just refresh data + open the dashboard), -NoDashboard, -BatchLimit N
# (max contacts per tier per run; the daily cap still wins), -Tiers top50,top500 (subset).
param(
    [switch]$SkipHarvest,
    [switch]$NoDashboard,
    [int]$BatchLimit = 0,
    [string[]]$Tiers = @('top50', 'top500', 'cat-a')
)

$ErrorActionPreference = 'Continue'
$env:PYTHONUTF8 = '1'          # names like 'Carla Muller' with accents print fine in any console
$env:PYTHONIOENCODING = 'utf-8'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
New-Item -ItemType Directory -Force -Path (Join-Path $repo 'data') | Out-Null
$log = Join-Path $repo 'data\run_kyc.log'
try { Start-Transcript -Path $log -Append | Out-Null } catch { }

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ""; Write-Host "!! $text" -ForegroundColor Red
    Write-Host "   Log: $log"
    try { Stop-Transcript | Out-Null } catch { }
    Read-Host "Press Enter to close"
    exit 1
}

# ---------------------------------------------------------------- 1. Python environment
Step "Python environment"
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "Creating .venv ..."
    $made = $false
    foreach ($cmd in @(@('py', '-3.11'), @('py', '-3'), @('python'))) {
        $exe = $cmd[0]; $rest = @($cmd | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        & $exe @rest -m venv .venv
        if ($LASTEXITCODE -eq 0 -and (Test-Path $py)) { $made = $true; break }
    }
    if (-not $made) { Fail "Couldn't create a virtual environment. Install Python 3.11+ from python.org (tick 'Add to PATH') and run this again." }
}
$reqHash = (Get-FileHash (Join-Path $repo 'requirements.txt') -Algorithm SHA256).Hash
$stamp = Join-Path $repo '.venv\requirements.sha256'
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $reqHash) {
    Write-Host "Installing requirements ..."
    & $py -m pip install --upgrade pip --quiet
    & $py -m pip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed (see above)." }
    Set-Content -Path $stamp -Value $reqHash
} else { Write-Host "Requirements up to date." }

$channel = (& $py -c "from src.config import load_config; print((load_config().get('harvest') or {}).get('browser_channel') or '')").Trim()
if (-not $SkipHarvest -and -not $channel) {
    & $py -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Fail "Playwright couldn't install Chromium. Or set harvest.browser_channel: chrome in config.yaml to use installed Chrome." }
}

# ---------------------------------------------------------------- 2-3. Ingest + faces
Step "Ingesting the workbooks from config.yaml"
& $py -m src.ingest
if ($LASTEXITCODE -ne 0) { Fail "Ingest failed - check inputs.relational_workbook / inputs.workbook in config.yaml (Google Drive for desktop must be running so G:\ is there)." }

Step "Matching the KYC face library"
& $py -m src.images
if ($LASTEXITCODE -ne 0) { Write-Host "(face matching reported a problem - continuing)" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 4. LinkedIn harvest, priority batches
if (-not $SkipHarvest) {
    foreach ($tier in $Tiers) {
        Step "LinkedIn harvest: $tier"
        $hargs = @('-m', 'src.harvest', '--tier', $tier, '--exit-code')
        if ($BatchLimit -gt 0) { $hargs += @('--limit', "$BatchLimit") }
        & $py @hargs
        $code = $LASTEXITCODE
        if ($code -eq 3) { Write-Host "Daily cap reached - the rest continues on the next run (tomorrow)." -ForegroundColor Yellow; break }
        if ($code -eq 2) { Write-Host "LinkedIn stopped the run (login / security check / limit). Re-run later; log in when the window opens." -ForegroundColor Yellow; break }
        if ($code -ne 0) { Write-Host "Harvester exited with code $code - continuing to export." -ForegroundColor Yellow; break }
    }
    Step "Refreshing face coverage"
    & $py -m src.images | Out-Null
}

# ---------------------------------------------------------------- 5. Export
Step "Exporting enriched contacts + faces"
& $py -m src.export
if ($LASTEXITCODE -ne 0) { Write-Host "(export reported a problem - continuing)" -ForegroundColor Yellow }

Step "KYC progress"
& $py -m src.dashboard --progress

# ---------------------------------------------------------------- 6. Dashboard
if (-not $NoDashboard) {
    $port = (& $py -c "from src.config import load_config; print((load_config().get('dashboard') or {}).get('port', 8765))").Trim()
    $url = "http://127.0.0.1:$port/graph/"
    $up = $false
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/api/kyc/progress" | Out-Null; $up = $true } catch { }
    if (-not $up) {
        Step "Starting the dashboard on port $port (leave its window open)"
        Start-Process -FilePath $py -ArgumentList @('-m', 'src.dashboard') -WorkingDirectory $repo
        for ($i = 0; $i -lt 30 -and -not $up; $i++) {
            Start-Sleep -Seconds 1
            try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/api/kyc/progress" | Out-Null; $up = $true } catch { }
        }
    } else { Write-Host "Dashboard already running." }
    Start-Process $url
    Write-Host "Opened $url  (People Tree + KYC progress: http://127.0.0.1:$port/)"
}

try { Stop-Transcript | Out-Null } catch { }
Write-Host ""
Write-Host "Done. Log: $log" -ForegroundColor Green
if ($Host.Name -eq 'ConsoleHost' -and -not $env:KYC_NO_PAUSE) { Read-Host "Press Enter to close" }
