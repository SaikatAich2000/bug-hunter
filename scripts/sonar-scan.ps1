# =============================================================================
#  scripts/sonar-scan.ps1
# -----------------------------------------------------------------------------
#  Windows / PowerShell version of scripts/sonar-scan.sh.
#
#  Usage:
#      .\scripts\sonar-scan.ps1
#
#  Override via env vars (set in the same shell BEFORE invoking):
#      $env:SONAR_HOST_URL = "http://localhost:9000"
#      $env:SONAR_TOKEN    = "sqp_xxxxxxxxxxxx"
#      .\scripts\sonar-scan.ps1
#
#  Database safety: same as the bash version - only writes coverage.xml,
#  junit.xml, and .scannerwork/ (all gitignored). No DB access.
# =============================================================================
$ErrorActionPreference = "Stop"

# --- Locate repo root (parent of this script) ------------------------------
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info  { param([string]$msg) Write-Host "[SONAR] $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "[WARN] $msg"  -ForegroundColor Yellow }
function Abort { param([string]$msg) Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

# --- Pre-flight ------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Abort "Docker is not on PATH. Install Docker Desktop and re-open this shell."
}
try { docker info *> $null } catch { Abort "Docker daemon is not running. Start Docker Desktop." }

$python = $null
foreach ($cmd in @("python", "py", "python3")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { $python = $cmd; break }
}
if (-not $python) { Abort "Python is not on PATH. Install Python 3.12 and re-open this shell." }

if (-not $env:SONAR_HOST_URL) { $env:SONAR_HOST_URL = "http://localhost:9000" }

# Reachability check
try {
    $resp = Invoke-WebRequest -Uri "$($env:SONAR_HOST_URL)/api/system/status" -TimeoutSec 5 -UseBasicParsing
    if ($resp.StatusCode -ne 200) { throw "non-200" }
} catch {
    Abort "Can't reach SonarQube at $($env:SONAR_HOST_URL). Is the container up? Try: docker start sonarqube"
}
Info "SonarQube is reachable at $($env:SONAR_HOST_URL)"

if (-not $env:SONAR_TOKEN) {
    Warn "No SONAR_TOKEN set - the scan will be submitted anonymously."
    Warn "If your SonarQube has 'Force user authentication' enabled (default"
    Warn "on recent versions), the scan WILL fail with 401."
    Warn "Generate a token at: $($env:SONAR_HOST_URL)/account/security/"
}

# --- Coverage + test report ------------------------------------------------
Info "Running pytest with coverage..."
Remove-Item -Force -ErrorAction SilentlyContinue coverage.xml, junit.xml

# Python 3.14 + pytest-cov: gzip / sqlite finalizers raise ResourceWarning
# straight from the C-level unraisablehook, which BYPASSES pyproject.toml's
# filterwarnings (those only catch `warnings.warn()` calls). Setting
# PYTHONWARNINGS muzzles them at the interpreter level. Saved data is
# unaffected - this only silences the noise.
$prevPyWarnings = $env:PYTHONWARNINGS
$env:PYTHONWARNINGS = "ignore"

try {
    & $python -m pytest `
        --cov=app `
        --cov-report=xml:coverage.xml `
        --cov-report=term-missing:skip-covered `
        --junitxml=junit.xml `
        -q
} catch {
    Warn "pytest exited non-zero - running scanner anyway so partial results land in SonarQube."
} finally {
    $env:PYTHONWARNINGS = $prevPyWarnings
}

# --- Sonar scan via Docker -------------------------------------------------
Info "Running sonar-scanner-cli via Docker..."

# Docker Desktop on Windows has host.docker.internal built-in, which is the
# clean way for a container to reach the host. Rewrite localhost in
# SONAR_HOST_URL so the scanner-container can find SonarQube-container.
$effectiveHost = $env:SONAR_HOST_URL
if ($effectiveHost -match '://(localhost|127\.0\.0\.1)(:|/|$)') {
    $effectiveHost = $effectiveHost -replace '://(localhost|127\.0\.0\.1)', '://host.docker.internal'
    Info "Rewriting SONAR_HOST_URL for the scanner container: $effectiveHost"
}

$scannerArgs = @(
    "run", "--rm",
    "-e", "SONAR_HOST_URL=$effectiveHost"
)
if ($env:SONAR_TOKEN) {
    $scannerArgs += @("-e", "SONAR_TOKEN=$env:SONAR_TOKEN")
}
$scannerArgs += @(
    "-v", "$($Root):/usr/src",
    "sonarsource/sonar-scanner-cli:latest"
)

& docker @scannerArgs
if ($LASTEXITCODE -ne 0) { Abort "sonar-scanner failed (exit $LASTEXITCODE)" }

Info "Done. Browse results at $($env:SONAR_HOST_URL)/dashboard?id=Bug_Hunter"
