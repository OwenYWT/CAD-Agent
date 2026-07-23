param([switch]$WhatIf)
$State = if ($env:CAD_AGENT_FUSION_STATE_DIR) { $env:CAD_AGENT_FUSION_STATE_DIR } else { Join-Path $env:USERPROFILE ".cad-agent\fusion360" }
$CurrentTarget = Join-Path $env:APPDATA "Autodesk\Autodesk Fusion\API\AddIns\CADAgentFusionConnector"
$LegacyTarget = Join-Path $env:APPDATA "Autodesk\Autodesk Fusion 360\API\AddIns\CADAgentFusionConnector"
if ($WhatIf) {
  Write-Host "WHATIF: stop optional Runtime; remove only $CurrentTarget and any legacy-path copy; preserve $State"
  exit 0
}
& (Join-Path $PSScriptRoot "stop-runtime.ps1")
foreach ($Target in @($CurrentTarget, $LegacyTarget)) {
  if (Test-Path $Target) { Remove-Item -Recurse -Force $Target }
}
Write-Host "Fusion Add-in removed. Connector configuration, audit journal, and credentials remain in $State."
