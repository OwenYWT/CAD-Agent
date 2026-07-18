param([switch]$WhatIf)
$State = if ($env:CAD_AGENT_FUSION_STATE_DIR) { $env:CAD_AGENT_FUSION_STATE_DIR } else { Join-Path $env:USERPROFILE ".cad-agent\fusion360" }
$PidFile = Join-Path $State "runtime.pid"
if ($WhatIf) { Write-Host "WHATIF: stop only PID recorded in $PidFile"; exit 0 }
if (-not (Test-Path $PidFile)) { Write-Host "Fusion Runtime is not running"; exit 0 }
$RuntimePid = [int](Get-Content $PidFile -Raw)
Stop-Process -Id $RuntimePid -ErrorAction SilentlyContinue
Remove-Item $PidFile -Force
Write-Host "Fusion Runtime stopped"
