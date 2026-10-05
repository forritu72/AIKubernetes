# Local DevOps Copilot launcher.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Error "No virtualenv at .\.venv - run: python -m venv .venv"
}

if (-not (Test-Path ".\.env")) {
    Write-Host "No .env found; copying .env.example (GitHub tools stay off until you add a PAT)." -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
}

& ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt

$port = 8099
$envPort = Select-String -Path ".env" -Pattern "^APP_PORT=(\d+)" -ErrorAction SilentlyContinue
if ($envPort) { $port = $envPort.Matches[0].Groups[1].Value }

Write-Host "DevOps Copilot -> http://127.0.0.1:$port" -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port $port
