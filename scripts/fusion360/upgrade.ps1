param(
  [switch]$WhatIf,
  [switch]$WithLocalRuntime
)
$Install = Join-Path $PSScriptRoot "install.ps1"
if ($WithLocalRuntime) {
  & (Join-Path $PSScriptRoot "stop-runtime.ps1") -WhatIf:$WhatIf
}
& $Install -WhatIf:$WhatIf -WithLocalRuntime:$WithLocalRuntime
if ($WithLocalRuntime) {
  & (Join-Path $PSScriptRoot "start-runtime.ps1") -WhatIf:$WhatIf
}
