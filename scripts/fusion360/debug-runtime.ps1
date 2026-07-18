param([switch]$WhatIf)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$State = if ($env:CAD_AGENT_FUSION_STATE_DIR) { $env:CAD_AGENT_FUSION_STATE_DIR } else { Join-Path $env:USERPROFILE ".cad-agent\fusion360" }
if ($WhatIf) { Write-Host "WHATIF: run Runtime foreground debug on loopback"; exit 0 }
$env:FUSION_RUNTIME_BACKEND_SECRET = (Get-Content (Join-Path $State "backend.secret") -Raw).Trim()
$env:FUSION_RUNTIME_CONNECTOR_SECRET = (Get-Content (Join-Path $State "connector.secret") -Raw).Trim()
$env:FUSION_RUNTIME_DB = Join-Path $State "runtime.db"
$env:FUSION_ARTIFACT_ROOT = Join-Path $State "artifacts"
$env:PYTHONPATH = Join-Path $Root "backend"
& (Join-Path $State "runtime-venv\Scripts\python.exe") -m uvicorn app.fusion360.runtime_app:app_from_env --factory --host 127.0.0.1 --port 8765 --log-level debug
