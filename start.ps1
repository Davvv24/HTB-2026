# Grid Advisor: one command to set up and run the whole app (Windows PowerShell).
#
#   .\start.ps1               set up if needed, then run on http://localhost:8000
#   .\start.ps1 -Offline      use saved data only (no live APIs), e.g. on weak Wi-Fi
#   .\start.ps1 -NoBrowser    don't open the browser
#   .\start.ps1 -Port 9000    use another port
#
# If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\start.ps1
# Needs Python 3.11+. Node.js 20+ is only needed when the web app has to be
# (re)built; if a built copy exists in frontend\dist it is used as-is.
param(
    [switch]$Offline,
    [switch]$NoBrowser,
    [int]$Port = 8000
)
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Say($message) { Write-Host "`n==> $message" }

# --- Python environment -------------------------------------------------------
# Same folder as the manual Windows setup, kept separate from any WSL .venv.
$venvPython = Join-Path $PSScriptRoot '.venv-win\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    $launcher = $null
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3.12') },
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python'; Args = @() }
    )
    foreach ($candidate in $candidates) {
        if (Get-Command $candidate.Exe -ErrorAction SilentlyContinue) {
            $checkArgs = $candidate.Args + @('-c', 'import sys; sys.exit(sys.version_info < (3, 11))')
            & $candidate.Exe @checkArgs 2>$null
            if ($LASTEXITCODE -eq 0) { $launcher = $candidate; break }
        }
    }
    if (-not $launcher) {
        Write-Host 'Python 3.11+ is needed. Install it from https://www.python.org/downloads/'
        exit 1
    }
    Say 'Creating the Python environment (.venv-win)'
    $venvArgs = $launcher.Args + @('-m', 'venv', '.venv-win')
    & $launcher.Exe @venvArgs
}

$wanted = (Get-FileHash -Algorithm SHA256 requirements.txt).Hash
$stamp = '.venv-win\.requirements-installed'
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $wanted) {
    Say 'Installing Python packages (first run takes a few minutes)'
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
    Set-Content -Path $stamp -Value $wanted
}

# --- Web app --------------------------------------------------------------------
$dist = 'frontend\dist\index.html'
$needsBuild = -not (Test-Path $dist)
if (-not $needsBuild) {
    $built = (Get-Item $dist).LastWriteTime
    $sources = @(Get-ChildItem -Recurse -File frontend\src) +
        @(Get-Item frontend\index.html, frontend\package.json, frontend\package-lock.json, frontend\vite.config.js)
    $needsBuild = [bool]($sources | Where-Object { $_.LastWriteTime -gt $built } | Select-Object -First 1)
}
if ($needsBuild) {
    if (Get-Command npm.cmd -ErrorAction SilentlyContinue) {
        Push-Location frontend
        try {
            $lock = 'node_modules\.package-lock.json'
            if (-not (Test-Path $lock) -or (Get-Item package-lock.json).LastWriteTime -gt (Get-Item $lock).LastWriteTime) {
                Say 'Installing web app packages'
                & npm.cmd ci --no-audit --no-fund --loglevel=error
                if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
            }
            Say 'Building the web app'
            & npm.cmd run build --silent
            if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
        } finally { Pop-Location }
    } elseif (Test-Path $dist) {
        Write-Host "Note: the web app has changes but Node.js isn't installed; using the existing build."
    } else {
        Write-Host 'The web app needs building once, which requires Node.js 20+ (https://nodejs.org/).'
        exit 1
    }
}

# --- Run ------------------------------------------------------------------------
$url = "http://localhost:$Port"
if ($Offline) {
    $env:GRID_ADVISOR_LIVE = '0'
    Say "Starting Grid Advisor on $url (offline: saved data only)"
} else {
    Remove-Item Env:GRID_ADVISOR_LIVE -ErrorAction SilentlyContinue
    Say "Starting Grid Advisor on $url (live data; first start can take ~10 s). Press Ctrl+C to stop."
}
if (-not $NoBrowser) {
    # Open the browser once the server answers (it loads live data before opening).
    Start-Job -ArgumentList $url -ScriptBlock {
        param($url)
        for ($i = 0; $i -lt 120; $i++) {
            try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 "$url/api/health" | Out-Null; Start-Process $url; break }
            catch { Start-Sleep -Milliseconds 500 }
        }
    } | Out-Null
}
& $venvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
