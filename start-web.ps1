# CAD Agent Web Windows setup/start script.
# Run from project root in PowerShell.
param(
    [ValidateSet("podman", "docker")]
    [string]$Runtime = "podman",
    [switch]$BuildFrontend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path "backend\.env")) {
    Copy-Item "backend\.env.example" "backend\.env"`n    $envContent = Get-Content -Raw -LiteralPath "backend\.env"`n    [System.IO.File]::WriteAllText((Resolve-Path "backend\.env"), $envContent, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Created backend\.env. Please edit Azure OpenAI values, then run again." -ForegroundColor Yellow
    exit 1
}

$envText = Get-Content "backend\.env" -Raw
if ($envText -match "replace-with-your-azure-openai-key") {
    Write-Host "Please edit backend\.env and set AZURE_OPENAI_API_KEY first." -ForegroundColor Yellow
    exit 1
}

Write-Host "Building sandbox image with $Runtime..." -ForegroundColor Cyan
Push-Location "backend\sandbox"
try {
    & $Runtime build -t cad-agent-sandbox:latest .
} finally {
    Pop-Location
}

Write-Host "Installing backend dependencies..." -ForegroundColor Cyan
Push-Location "backend"
try {
    python -m pip install -r requirements.txt
} finally {
    Pop-Location
}

if ($BuildFrontend) {
    Write-Host "Installing frontend dependencies and building Web UI..." -ForegroundColor Cyan
    Push-Location "frontend"
    try {
        npm.cmd ci
        npm.cmd run build
    } finally {
        Pop-Location
    }
}

$env:SANDBOX_RUNTIME = $Runtime
Write-Host "Starting backend at http://localhost:8000" -ForegroundColor Green
Set-Location "backend"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

