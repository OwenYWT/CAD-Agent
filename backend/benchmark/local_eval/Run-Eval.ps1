<#
.SYNOPSIS
    Reproduce the CAD-Agent visual-gate benchmark: accuracy and latency.

.DESCRIPTION
    Runs every case in backend/benchmark/eval_cases.py through the real
    pipeline -- real planner, real code generation, real CadQuery kernel, real
    geometry validation, real VLM visual gate -- and scores the result with the
    repository's own metrics. Then prints the accuracy table and the per-case
    wall-clock / execution-time distribution.

    Case expectations are used for SCORING ONLY. The pipeline sees each
    description exactly as a user request would arrive.

    This harness substitutes ONE thing: the isolation boundary. Generated CAD is
    executed in a killable local subprocess rather than the sandbox container,
    because this is meant to run on a workstation with no container runtime. If
    you have Docker or Podman, the real harness is:
        docker build -f backend/sandbox/Dockerfile -t cad-agent-sandbox:dev .
        cd backend; python -m benchmark.eval --n 3

.PARAMETER N
    Repeats per case. 1 gives pass@1. 3 additionally gives pass@k / pass^k and
    is what you want before trusting a number, since the pipeline is stochastic.

.PARAMETER Difficulty
    Restrict to one tier: simple, moderate or complex. Default runs all.

.PARAMETER Cases
    Comma-separated case ids (for example P01,M03). Default runs all.

.PARAMETER NoVisual
    Ablation: disable the VLM refinement loop, to measure its contribution.

.PARAMETER Concurrency
    Cases in flight at once. Raising this shortens elapsed time but inflates the
    reported per-case wall clock, because cases then queue behind each other.

.PARAMETER Provider
    Which model preset to evaluate against.
      env            - use whatever backend/.env already configures (default)
      openai         - OpenAI gpt-5.4, needs OPENAI_API_KEY
      dashscope      - DashScope Qwen, needs DASHSCOPE_API_KEY
      dashscope-kimi - Moonshot Kimi k3 for language, Qwen for vision,
                       both served through DashScope
    Selecting a preset sets LLM_PROVIDER, LLM_MODEL, VISION_MODEL, LLM_BASE_URL
    and LLM_TIMEOUT_S as process environment variables, which take precedence
    over backend/.env. The API key itself is never read by this script:
    Settings loads .env.

.PARAMETER Model
    Override the planning / code-generation model for the chosen provider.

.PARAMETER VisionModel
    Override the model used by the visual gate. It must accept image input, and
    it also writes the repaired source, so it needs to be competent at code.

.EXAMPLE
    .\Run-Eval.ps1
    .\Run-Eval.ps1 -N 3 -Concurrency 5
    .\Run-Eval.ps1 -Difficulty moderate
    .\Run-Eval.ps1 -Provider dashscope
    .\Run-Eval.ps1 -NoVisual -Out ablation.json
#>
[CmdletBinding()]
param(
    [int]$N = 1,
    [ValidateSet('all', 'simple', 'moderate', 'complex')]
    [string]$Difficulty = 'all',
    [string]$Cases = 'all',
    [switch]$NoVisual,
    [int]$Concurrency = 5,
    [int]$DeadlineSeconds = 900,
    [string]$Out = '',
    [ValidateSet('env', 'openai', 'dashscope', 'dashscope-kimi')]
    [string]$Provider = 'env',
    [string]$Model = '',
    [string]$VisionModel = '',
    [switch]$InstallDeps,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'

function Write-Section($text) {
    Write-Host ''
    Write-Host ('=' * 78) -ForegroundColor DarkCyan
    Write-Host $text -ForegroundColor Cyan
    Write-Host ('=' * 78) -ForegroundColor DarkCyan
}

function Write-Step($text) { Write-Host "  $text" -ForegroundColor Gray }
function Write-Ok($text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Write-Bad($text) { Write-Host "  [x]  $text" -ForegroundColor Red }

function Stop-Here($code) {
    if (-not $NoPause) {
        Write-Host ''
        Write-Host 'Press Enter to close...' -ForegroundColor DarkGray
        [void](Read-Host)
    }
    exit $code
}

Write-Section 'CAD-Agent benchmark - accuracy and latency'

# --- paths -------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$RepoRoot = Split-Path -Parent $BackendDir

if (-not (Test-Path (Join-Path $BackendDir 'benchmark\eval_cases.py'))) {
    Write-Bad "Cannot locate the backend directory from $ScriptDir"
    Write-Step 'Expected this script at backend\benchmark\local_eval\Run-Eval.ps1'
    Stop-Here 1
}
Write-Step "repo    : $RepoRoot"
Write-Step "backend : $BackendDir"

# --- interpreter -------------------------------------------------------------
$Python = $null
foreach ($candidate in @('python', 'py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $Python = $found.Source; break }
}
if (-not $Python) {
    Write-Bad 'No Python interpreter found on PATH.'
    Write-Step 'Install Python 3.11 or newer, then re-run.'
    Stop-Here 1
}

$versionText = & $Python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Step "python  : $Python (v$versionText)"

$parts = $versionText.Split('.')
$major = [int]$parts[0]
$minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
    Write-Bad "Python $versionText is too old; 3.11+ is expected (3.10 works via a shim)."
    Stop-Here 1
}

# The application targets 3.11+. On 3.10 a tiny sitecustomize shim backfills
# datetime.UTC and enum.StrEnum so the codebase imports; nothing else differs.
$ShimPath = ''
if ($major -eq 3 -and $minor -eq 10) {
    $ShimPath = Join-Path $ScriptDir 'py311_shim'
    Write-Warn2 "Python 3.10 detected - enabling the 3.11 compatibility shim."
}

# --- dependencies ------------------------------------------------------------
Write-Section 'Checking dependencies'
$modules = [ordered]@{
    'cadquery'          = 'cadquery'
    'trimesh'           = 'trimesh'
    'numpy'             = 'numpy'
    'PIL'               = 'pillow'
    'matplotlib'        = 'matplotlib'
    'openai'            = 'openai'
    'pydantic'          = 'pydantic'
    'pydantic_settings' = 'pydantic-settings'
    'sklearn'           = 'scikit-learn'
    'ezdxf'             = 'ezdxf'
    'aiosqlite'         = 'aiosqlite'
    'manifold3d'        = 'manifold3d'
}
$missing = @()
foreach ($module in $modules.Keys) {
    & $Python -c "import $module" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok $module
    }
    else {
        Write-Bad "$module (pip: $($modules[$module]))"
        $missing += $modules[$module]
    }
}

if ($missing.Count -gt 0) {
    Write-Host ''
    Write-Warn2 "Missing: $($missing -join ', ')"
    # Only the missing packages are installed, and unpinned. Installing
    # backend/requirements.txt wholesale would downgrade numpy and can break an
    # otherwise working environment.
    $doInstall = $InstallDeps
    if (-not $doInstall) {
        $answer = Read-Host '  Install them now? [y/N]'
        if ($answer -match '^[Yy]') { $doInstall = $true }
    }
    if ($doInstall) {
        & $Python -m pip install @missing
        if ($LASTEXITCODE -ne 0) {
            Write-Bad 'pip install failed.'
            Stop-Here 1
        }
    }
    else {
        Write-Step "Install with:  $Python -m pip install $($missing -join ' ')"
        Stop-Here 1
    }
}

# --- provider ----------------------------------------------------------------
Write-Section 'Selecting provider'
$EnvFile = Join-Path $BackendDir '.env'

# Defaults are the strongest generally-available model on each provider that
# accepts image input. The vision model both inspects renders AND rewrites the
# source, so it has to be good at code as well as pictures.
# Timeout is per provider because Qwen's reasoning models emit thousands of
# thinking tokens per call and legitimately need minutes, where the OpenAI
# default of 180s is generous.
# These are PRESET names, not provider names: 'dashscope-kimi' routes Moonshot's
# Kimi through DashScope, so the preset it selects and the LLM_PROVIDER it sets
# are deliberately different values.
$providerDefaults = @{
    'openai'         = @{ Provider = 'openai';    Key = 'OPENAI_API_KEY';    Model = 'gpt-5.4';          Vision = 'gpt-5.4';       Timeout = 180 }
    'dashscope'      = @{ Provider = 'dashscope'; Key = 'DASHSCOPE_API_KEY'; Model = 'qwen3-coder-plus'; Vision = 'qwen3-vl-plus'; Timeout = 420 }
    # Kimi has no vision variant on DashScope, so the visual gate stays on Qwen
    # while planning and code generation move to Kimi.
    'dashscope-kimi' = @{ Provider = 'dashscope'; Key = 'DASHSCOPE_API_KEY'; Model = 'kimi-k3';          Vision = 'qwen3-vl-plus'; Timeout = 420 }
}

$requiredKey = $null
if ($Provider -eq 'env') {
    Write-Step 'using the provider configured in backend\.env'
}
else {
    $chosen = $providerDefaults[$Provider]
    $requiredKey = $chosen.Key

    $useModel = $Model
    if ([string]::IsNullOrWhiteSpace($useModel)) { $useModel = $chosen.Model }
    $useVision = $VisionModel
    if ([string]::IsNullOrWhiteSpace($useVision)) { $useVision = $chosen.Vision }

    # Process-scoped, and precedence over .env: pydantic-settings reads real
    # environment variables ahead of the env_file.
    $env:LLM_PROVIDER = $chosen.Provider
    $env:LLM_MODEL = $useModel
    $env:VISION_MODEL = $useVision
    # A stale LLM_BASE_URL in .env would otherwise point the new provider's key
    # at the old provider's host. config.py substitutes the right default when
    # this is empty.
    $env:LLM_BASE_URL = ''
    $env:LLM_TIMEOUT_S = $chosen.Timeout

    Write-Ok "preset      : $Provider"
    Write-Ok "provider    : $($chosen.Provider)"
    Write-Ok "model       : $useModel"
    Write-Ok "vision model: $useVision"
    Write-Ok "call timeout: $($chosen.Timeout)s"
}

# --- credentials -------------------------------------------------------------
Write-Section 'Checking model credentials'
$haveKey = $false
$keyPattern = '^\s*(OPENAI_API_KEY|MOONSHOT_API_KEY|AZURE_OPENAI_API_KEY|DASHSCOPE_API_KEY)\s*=\s*\S'
if ($requiredKey) { $keyPattern = "^\s*$requiredKey\s*=\s*\S" }

if ($requiredKey -and (Get-Item "env:$requiredKey" -ErrorAction SilentlyContinue)) {
    Write-Ok "$requiredKey found in the environment"
    $haveKey = $true
}
elseif ((-not $requiredKey) -and $env:OPENAI_API_KEY) {
    Write-Ok 'OPENAI_API_KEY found in the environment'
    $haveKey = $true
}
elseif (Test-Path $EnvFile) {
    # Settings loads backend/.env itself (config.py: env_file='.env'), so the
    # key is never read into this script or echoed anywhere.
    if (Select-String -Path $EnvFile -Pattern $keyPattern -Quiet) {
        Write-Ok "credential found in backend\.env"
        $haveKey = $true
    }
}

if (-not $haveKey) {
    Write-Bad 'No usable model credentials found.'
    if ($requiredKey) {
        Write-Step "Provider '$Provider' needs $requiredKey."
        Write-Step "Add it to $EnvFile (gitignored) or set `$env:$requiredKey."
    }
    else {
        Write-Step "Create $EnvFile with at least:"
        Write-Step '    LLM_PROVIDER=openai'
        Write-Step '    OPENAI_API_KEY=<your key>'
        Write-Step '    LLM_MODEL=gpt-5.4'
        Write-Step '    VISION_MODEL=gpt-5.4'
        Write-Step 'or set $env:OPENAI_API_KEY before running. That file is gitignored.'
    }
    Stop-Here 1
}

# --- run ---------------------------------------------------------------------
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$reportDir = Join-Path $ScriptDir 'reports'
if (-not (Test-Path $reportDir)) { [void](New-Item -ItemType Directory -Path $reportDir) }

if ([string]::IsNullOrWhiteSpace($Out)) {
    $suffix = 'visual'
    if ($NoVisual) { $suffix = 'novisual' }
    # The provider is in the filename because reports from different providers
    # are not comparable and get mixed up in the reports directory otherwise.
    $Out = Join-Path $reportDir "eval-$stamp-$Provider-$suffix.json"
}
$logPath = [System.IO.Path]::ChangeExtension($Out, '.log')

$arguments = @(
    (Join-Path $ScriptDir 'run_eval.py'),
    '--n', $N,
    '--concurrency', $Concurrency,
    '--cases', $Cases,
    '--out', $Out
)
if ($Difficulty -ne 'all') { $arguments += @('--difficulty', $Difficulty) }
if ($NoVisual) { $arguments += '--no-visual' }

Write-Section 'Running evaluation'
Write-Step "cases      : $Cases    difficulty: $Difficulty    repeats: $N"
Write-Step "concurrency: $Concurrency    deadline: ${DeadlineSeconds}s"
Write-Step "report     : $Out"
Write-Host ''
Write-Warn2 'This makes real, billable model calls and takes roughly 15-25 minutes'
Write-Warn2 'for the full 50-case set. Progress prints one line per case.'
Write-Host ''

# Scoped to this process only; nothing is written to the user profile.
$env:PYTHONIOENCODING = 'utf-8'
$env:GENERATE_DEADLINE_S = $DeadlineSeconds
if ($ShimPath) {
    $env:PYTHONPATH = "$ShimPath;$BackendDir"
}
else {
    $env:PYTHONPATH = $BackendDir
}

# Settings resolves .env relative to the working directory, so run from backend.
Push-Location $BackendDir
$started = Get-Date

# Windows PowerShell wraps each stderr line of a native command in an
# ErrorRecord, which under $ErrorActionPreference='Stop' aborts the run on the
# first log line Python emits. Relax it just for this call. Lines are also
# written through Out-File -Encoding utf8 rather than Tee-Object, which would
# produce a UTF-16 log that other tools cannot read.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
if (Test-Path $logPath) { Remove-Item $logPath -Force }
try {
    & $Python @arguments 2>&1 | ForEach-Object {
        $line = $_.ToString()
        Write-Host $line
        $line | Out-File -FilePath $logPath -Append -Encoding utf8
    }
    $evalExit = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousPreference
    Pop-Location
}
$elapsed = (Get-Date) - $started

if ($evalExit -ne 0) {
    Write-Bad "Evaluation exited with code $evalExit. See $logPath"
    Stop-Here $evalExit
}

# --- report ------------------------------------------------------------------
Write-Section 'Accuracy and latency'
Push-Location $BackendDir
try {
    & $Python (Join-Path $ScriptDir 'report_timing.py') $Out
}
finally {
    Pop-Location
}

Write-Host ''
Write-Ok ("elapsed: {0:hh\:mm\:ss}" -f $elapsed)
Write-Ok "report : $Out"
Write-Ok "log    : $logPath"
Write-Host ''
Write-Step 'Re-print this report later with:'
Write-Step "  $Python benchmark\local_eval\report_timing.py <report.json>"
Write-Step 'Measure the visual gate contribution with:  .\Run-Eval.ps1 -NoVisual'

Stop-Here 0
