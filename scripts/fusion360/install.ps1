param(
  [switch]$WhatIf,
  [switch]$WithLocalRuntime
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$State = if ($env:CAD_AGENT_FUSION_STATE_DIR) { $env:CAD_AGENT_FUSION_STATE_DIR } else { Join-Path $env:USERPROFILE ".cad-agent\fusion360" }
$Addins = Join-Path $env:APPDATA "Autodesk\Autodesk Fusion\API\AddIns"
$Target = Join-Path $Addins "CADAgentFusionConnector"
Write-Host "Fusion connector user install (direct HTTPS Cloud Agent mode): $Target"
if ($WhatIf) {
  Write-Host "WHATIF: create user-only Agent configuration and atomically replace Add-in"
  if ($WithLocalRuntime) { Write-Host "WHATIF: also provision the optional loopback Runtime and independent role secrets" }
  exit 0
}

New-Item -ItemType Directory -Force -Path $Addins, (Join-Path $State "artifacts") | Out-Null
if ($WithLocalRuntime) {
  python -m venv (Join-Path $State "runtime-venv")
  & (Join-Path $State "runtime-venv\Scripts\python.exe") -m pip install --disable-pip-version-check -r (Join-Path $Root "scripts\fusion360\runtime-requirements.txt")
  foreach ($Name in @("backend.secret", "connector.secret")) {
    $Path = Join-Path $State $Name
    if (-not (Test-Path $Path)) {
      $Bytes = New-Object byte[] 48
      [Security.Cryptography.RandomNumberGenerator]::Fill($Bytes)
      [IO.File]::WriteAllText($Path, [Convert]::ToBase64String($Bytes))
    }
  }
}

$Token = Join-Path $State "agent.token"
if (-not (Test-Path $Token)) { [IO.File]::WriteAllText($Token, "") }
$Config = Join-Path $State "connector.json"
if (-not (Test-Path $Config)) {
  $Template = Get-Content (Join-Path $Root "fusion_addin\config.example.json") -Raw
  $Template = $Template.Replace('/replace/with/user-only/config/path/agent.token', $Token.Replace('\','/'))
  $Template = $Template.Replace('/replace/with/user-only/config/path/connector.secret', (Join-Path $State 'connector.secret').Replace('\','/'))
  $Template = $Template.Replace('replace-with-a-stable-uuid', [guid]::NewGuid().ToString())
  $Template = $Template.Replace('/replace/with/user/data/path/artifacts', (Join-Path $State 'artifacts').Replace('\','/'))
  $Template = $Template.Replace('/replace/with/user/data/path/execution-journal.json', (Join-Path $State 'execution-journal.json').Replace('\','/'))
  [IO.File]::WriteAllText($Config, $Template)
}

$Principal = if ($env:USERDOMAIN) { "$($env:USERDOMAIN)\$($env:USERNAME)" } else { $env:USERNAME }
$SensitivePaths = @($Token, $Config)
if ($WithLocalRuntime) {
  $SensitivePaths += (Join-Path $State "backend.secret"), (Join-Path $State "connector.secret")
}
foreach ($SensitivePath in $SensitivePaths) {
  & icacls.exe $SensitivePath /inheritance:r /grant:r "${Principal}:F" | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Failed to restrict ACL for $SensitivePath" }
}

$Backup = Join-Path $State "addin-backup"
if (Test-Path $Backup) { Remove-Item -Recurse -Force $Backup }
if (Test-Path $Target) { Move-Item $Target $Backup }
try {
  Copy-Item -Recurse (Join-Path $Root "fusion_addin\CADAgentFusionConnector") $Target
  if (Test-Path $Backup) { Remove-Item -Recurse -Force $Backup }
} catch {
  if (Test-Path $Backup) { Move-Item $Backup $Target }
  throw
}

Write-Host "Installed. Configure agent_url and put the bearer token in $Token."
Write-Host "Restart Fusion, open Utilities > Add-Ins, and run CADAgentFusionConnector."
if ($WithLocalRuntime) { Write-Host "Local Runtime was installed but is not selected; set mode=local_runtime before starting it." }
