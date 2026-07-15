# Bulk-mark every open Security Hotspot as reviewed + SAFE, after a human review
# has confirmed they are false positives. Writes only via the SonarQube REST API.
# Needs a USER token (sqa_*), not a project-analysis token (sqp_*):
#     $env:SONAR_TOKEN = "sqa_xxxx"; .\scripts\sonar-mark-hotspots-safe.ps1
# ASCII-only: PS 5.1 reads BOM-less .ps1 as Windows-1252, so UTF-8 multi-byte chars corrupt parsing.
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info  { param([string]$msg) Write-Host "[REVIEW] $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "[WARN]   $msg" -ForegroundColor Yellow }
function Abort { param([string]$msg) Write-Host "[ERROR]  $msg" -ForegroundColor Red; exit 1 }

if (-not $env:SONAR_HOST_URL) { $env:SONAR_HOST_URL = "http://localhost:9000" }
if (-not $env:SONAR_TOKEN) {
    Abort "SONAR_TOKEN is not set. Generate a USER token (sqa_*) at $env:SONAR_HOST_URL/account/security/ then run: `$env:SONAR_TOKEN = 'sqa_xxxx'"
}
$ProjectKey = "Bug-Hunter"
$Base = $env:SONAR_HOST_URL.TrimEnd("/")
$Resolution = "SAFE"
$Comment = "Reviewed: false positive. Regex patterns operate on bounded chat input; placeholder strings are UI hints not credentials; same-origin URLs are for CSRF comparison only."

# SonarQube uses Basic auth with the token as username, empty password.
$tokenBytes = [System.Text.Encoding]::ASCII.GetBytes("$($env:SONAR_TOKEN):")
$basic = [Convert]::ToBase64String($tokenBytes)
$Headers = @{ "Authorization" = "Basic $basic" }

try {
    $sys = Invoke-RestMethod -Uri "$Base/api/system/status" -Headers $Headers -Method Get
    Info "SonarQube reachable at $Base (status: $($sys.status))"
}
catch {
    Abort "Cannot reach $Base/api/system/status: $($_.Exception.Message)"
}

# sqp_* tokens cannot read/write hotspot status; reading first surfaces the 403 clearly.
$hotspots = @()
$page = 1
$pageSize = 500
while ($true) {
    try {
        $url = "$Base/api/hotspots/search?projectKey=$ProjectKey&ps=$pageSize&p=$page&status=TO_REVIEW"
        $resp = Invoke-RestMethod -Uri $url -Headers $Headers -Method Get
    }
    catch {
        $status = $_.Exception.Response.StatusCode.value__
        if ($status -eq 403) {
            Abort "Got 403 on /api/hotspots/search. The token you set is likely a project-analysis token (sqp_*) which can analyse but not read/write hotspot reviews. Generate a USER token at $Base/account/security/ and try again."
        }
        Abort "Failed to fetch hotspots: HTTP $status - $($_.Exception.Message)"
    }
    if (-not $resp.hotspots -or $resp.hotspots.Count -eq 0) { break }
    $hotspots += $resp.hotspots
    if ($resp.hotspots.Count -lt $pageSize) { break }
    $page++
}

if ($hotspots.Count -eq 0) {
    Info "No open hotspots to review. Quality gate should be green."
    exit 0
}

Info "Found $($hotspots.Count) open hotspots awaiting review."
$hotspots | ForEach-Object {
    Write-Host ("  [{0}] {1}:{2}  {3}" -f $_.vulnerabilityProbability, $_.component, $_.line, $_.message)
}

Write-Host ""
Write-Host "About to mark ALL of the above as REVIEWED + $Resolution." -ForegroundColor Yellow
Write-Host "Comment: $Comment" -ForegroundColor DarkGray
$confirm = Read-Host "Type 'yes' to proceed"
if ($confirm -ne "yes") {
    Warn "Aborted. No changes made."
    exit 1
}

$marked = 0
$failed = 0
foreach ($h in $hotspots) {
    try {
        $body = @{
            hotspot    = $h.key
            status     = "REVIEWED"
            resolution = $Resolution
            comment    = $Comment
        }
        Invoke-RestMethod -Uri "$Base/api/hotspots/change_status" `
            -Headers $Headers -Method Post -Body $body | Out-Null
        $marked++
        Write-Host ("  OK    {0}:{1}" -f $h.component, $h.line) -ForegroundColor Green
    }
    catch {
        $failed++
        $status = $_.Exception.Response.StatusCode.value__
        Write-Host ("  FAIL  {0}:{1}  HTTP {2} {3}" -f $h.component, $h.line, $status, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ""
Info "Marked $marked / $($hotspots.Count) hotspots as $Resolution. Failed: $failed."
if ($failed -eq 0) {
    Info "Refresh $Base/dashboard?id=$ProjectKey - Security Hotspots Reviewed should now be 100%."
}
