param([switch]$WhatIf)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$State = if ($env:CAD_AGENT_FUSION_STATE_DIR) { $env:CAD_AGENT_FUSION_STATE_DIR } else { Join-Path $env:USERPROFILE ".cad-agent\fusion360" }
if ($WhatIf) { Write-Host "WHATIF: start Runtime on 127.0.0.1:8765"; exit 0 }
$env:FUSION_RUNTIME_BACKEND_SECRET = (Get-Content (Join-Path $State "backend.secret") -Raw).Trim()
$env:FUSION_RUNTIME_CONNECTOR_SECRET = (Get-Content (Join-Path $State "connector.secret") -Raw).Trim()
$env:FUSION_RUNTIME_DB = Join-Path $State "runtime.db"
$env:FUSION_ARTIFACT_ROOT = Join-Path $State "artifacts"
$env:PYTHONPATH = Join-Path $Root "backend"
try {
  $Process = Start-Process -PassThru -WindowStyle Hidden -FilePath (Join-Path $State "runtime-venv\Scripts\python.exe") -ArgumentList @("-m","uvicorn","app.fusion360.runtime_app:app_from_env","--factory","--host","127.0.0.1","--port","8765") -RedirectStandardOutput (Join-Path $State "runtime.log") -RedirectStandardError (Join-Path $State "runtime-error.log")
} finally {
  Remove-Item Env:FUSION_RUNTIME_BACKEND_SECRET -ErrorAction SilentlyContinue
  Remove-Item Env:FUSION_RUNTIME_CONNECTOR_SECRET -ErrorAction SilentlyContinue
}
[IO.File]::WriteAllText((Join-Path $State "runtime.pid"), $Process.Id.ToString())
Start-Sleep -Seconds 1
Invoke-RestMethod "http://127.0.0.1:8765/health" | Out-Null
Write-Host "Fusion Runtime started (PID $($Process.Id))"
