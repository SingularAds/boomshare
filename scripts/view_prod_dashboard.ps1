<#
.SYNOPSIS
  Open the dashboard on your machine, reading the production database.

.DESCRIPTION
  Production's DATABASE_URL is a Cloud SQL unix socket, which only resolves
  inside Cloud Run. This starts the Cloud SQL Auth Proxy, rewrites the URL to
  the TCP address the proxy listens on, and runs the API locally against it.

  READ-ONLY BY INTENT. The embedded worker is switched off, so this process
  never consumes the job queue - it will not call OpenAI and will not send a
  WhatsApp message to a real customer. Leave that flag alone: with the worker
  running, a laptop pointed at production starts answering live conversations.

.PREREQUISITE
  One interactive step, which only you can do:

      gcloud auth application-default login

.EXAMPLE
  .\scripts\view_prod_dashboard.ps1
  # then open http://localhost:8001/dashboard
#>

param(
    [int]$ProxyPort = 6543,
    [int]$ApiPort = 8001
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$instance = "smbaicallz:southamerica-east1:boom-share"
$proxy = Join-Path $repo "tools\cloud-sql-proxy.exe"

if (-not (Test-Path $proxy)) {
    throw "cloud-sql-proxy not found at $proxy"
}

# --- credentials ----------------------------------------------------------
$adc = Join-Path $env:APPDATA "gcloud\application_default_credentials.json"
if (-not (Test-Path $adc)) {
    Write-Host ""
    Write-Host "Not authenticated to Google Cloud." -ForegroundColor Yellow
    Write-Host "Run this once, then re-run this script:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    gcloud auth application-default login" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

# --- build the local environment from .env.production ----------------------
$envFile = Join-Path $repo ".env.production"
if (-not (Test-Path $envFile)) { throw "missing .env.production" }

$settings = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Z_][A-Z0-9_]*)=(.*)$') {
        $settings[$Matches[1]] = $Matches[2].Trim()
    }
}

# Swap the unix socket for the proxy's TCP address, keeping user/password/db.
$dbUrl = $settings["DATABASE_URL"]
if ($dbUrl -notmatch '^(?<scheme>[^:]+)://(?<creds>[^@]+)@/(?<db>[^?]+)') {
    throw "DATABASE_URL is not in the Cloud SQL socket form this script expects"
}
$settings["DATABASE_URL"] =
    "$($Matches.scheme)://$($Matches.creds)@127.0.0.1:$ProxyPort/$($Matches.db)"

# Nothing in this process may reach a customer.
$settings["RUN_EMBEDDED_WORKER"] = "false"
$settings["ENVIRONMENT"] = "local"
$settings["LOG_JSON"] = "false"
$settings["LOG_LEVEL"] = "INFO"
# Redis is not on the dashboard's path; only /health/ready will complain.
if (-not $settings["REDIS_URL"]) { $settings["REDIS_URL"] = "redis://localhost:6379/0" }

foreach ($key in $settings.Keys) {
    Set-Item -Path "env:$key" -Value $settings[$key]
}

# --- proxy ------------------------------------------------------------------
Write-Host "Starting Cloud SQL proxy on 127.0.0.1:$ProxyPort ..." -ForegroundColor Cyan
$proxyProcess = Start-Process -FilePath $proxy `
    -ArgumentList @($instance, "--port", "$ProxyPort") `
    -PassThru -NoNewWindow

try {
    $ready = $false
    foreach ($attempt in 1..30) {
        Start-Sleep -Milliseconds 500
        if ($proxyProcess.HasExited) { throw "the proxy exited - check the error above" }
        $probe = Test-NetConnection -ComputerName 127.0.0.1 -Port $ProxyPort `
            -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($probe) { $ready = $true; break }
    }
    if (-not $ready) { throw "proxy did not accept connections on port $ProxyPort" }

    Write-Host "Proxy is up." -ForegroundColor Green
    Write-Host ""
    Write-Host "  Dashboard : http://localhost:$ApiPort/dashboard" -ForegroundColor Green
    Write-Host "  Sign in with the ADMIN_API_TOKEN from .env.production" -ForegroundColor Green
    Write-Host "  Worker    : OFF - this process cannot message a customer" -ForegroundColor Yellow
    Write-Host ""

    python -m uvicorn app.main:app --port $ApiPort --host 127.0.0.1
}
finally {
    if ($proxyProcess -and -not $proxyProcess.HasExited) {
        Write-Host "Stopping the proxy ..." -ForegroundColor Cyan
        Stop-Process -Id $proxyProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
