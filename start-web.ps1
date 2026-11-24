# CAD Agent Web Windows setup/start script.
# Run from project root in PowerShell.
param(
    [ValidateSet("docker", "podman")]
    [string]$Runtime = "docker",
    [switch]$BuildFrontend,
    [switch]$SkipSandboxBuild,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Set-EnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $content = Get-Content -Raw -LiteralPath $Path
    $line = "$Name=$Value"
    if ($content -match "(?m)^$([regex]::Escape($Name))=") {
        $content = $content -replace "(?m)^$([regex]::Escape($Name))=.*$", $line
    } else {
        if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) {
            $content += "`n"
        }
        $content += "$line`n"
    }
    [System.IO.File]::WriteAllText((Resolve-Path -LiteralPath $Path), $content, [System.Text.UTF8Encoding]::new($false))
}

$envPath = "backend\.env"
if (-not (Test-Path $envPath)) {
    Copy-Item "backend\.env.example" $envPath
    $envContent = Get-Content -Raw -LiteralPath $envPath
    [System.IO.File]::WriteAllText((Resolve-Path -LiteralPath $envPath), $envContent, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Created backend\.env from backend\.env.example." -ForegroundColor Yellow
    Write-Host "You can configure LLM credentials later; the backend will start in degraded mode until then." -ForegroundColor Yellow
}

Set-EnvValue -Path $envPath -Name "SANDBOX_RUNTIME" -Value $Runtime
Set-EnvValue -Path $envPath -Name "SANDBOX_COMMAND" -Value $Runtime

$runtimeCommand = Get-Command $Runtime -ErrorAction SilentlyContinue
if (-not $runtimeCommand) {
    throw "'$Runtime' was not found on PATH. Install/start $Runtime, or run .\start-web.ps1 -Runtime podman."
}

if ($Runtime -eq "docker") {
    try {
        docker info *> $null
    } catch {
        throw "Docker is installed but the daemon is unavailable. Start Docker Desktop, then run this script again."
    }
}

if (-not $SkipInstall) {
    Write-Host "Installing backend dependencies..." -ForegroundColor Cyan
    Push-Location "backend"
    try {
        python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed." }
    } finally {
        Pop-Location
    }
}

Push-Location "backend"
try {
    $env:SANDBOX_IMAGE = python -c 'from app.config import settings; print(settings.sandbox_image)'
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve the configured sandbox image." }
} finally {
    Pop-Location
}

if (-not $SkipSandboxBuild) {
    Write-Host "Building $env:SANDBOX_IMAGE with $Runtime..." -ForegroundColor Cyan
    & $Runtime build -f backend/sandbox/Dockerfile -t $env:SANDBOX_IMAGE .
    if ($LASTEXITCODE -ne 0) { throw "Sandbox image build failed (exit $LASTEXITCODE)." }
}

if ($BuildFrontend) {
    Write-Host "Installing frontend dependencies and building Web UI..." -ForegroundColor Cyan
    Push-Location "frontend"
    try {
        if (Test-Path "package-lock.json") {
            npm.cmd ci
        } else {
            npm.cmd install
        }
        npm.cmd run build
    } finally {
        Pop-Location
    }
}

$env:SANDBOX_RUNTIME = $Runtime
$env:SANDBOX_COMMAND = $Runtime
Set-Location "backend"
python -c 'from app.execution.composition import get_execution_backend; get_execution_backend().runtime_snapshot()'
if ($LASTEXITCODE -ne 0) { throw "FreeCAD sandbox $env:SANDBOX_IMAGE failed the backend runtime check." }

Write-Host "Starting backend at http://localhost:8000" -ForegroundColor Green
Write-Host "Readiness endpoint: http://localhost:8000/ready" -ForegroundColor Green
Write-Host "If LLM credentials are not configured yet, /ready will report degraded but /health should be ok." -ForegroundColor Yellow
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
