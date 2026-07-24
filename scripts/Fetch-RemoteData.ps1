#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Sync logs/ and data/ directories from remote server via SSH key authentication.
    Uses native Windows SSH (OpenSSH) — no external dependencies.

.DESCRIPTION
    Reads REMOTE_* credentials from .env and syncs new/modified files only.
    Exits with code 1 on failure (blocks agent runs without data).
#>

param(
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

# Get project root (script is in scripts/ subdirectory)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $ProjectRoot $EnvFile

if (-not (Test-Path $EnvPath)) {
    Write-Host "ERROR: .env file not found at $EnvPath" -ForegroundColor Red
    Write-Host "Create one by copying .env.example and filling in REMOTE_* values." -ForegroundColor Red
    exit 1
}

# Parse .env file
$env_vars = @{}
Get-Content $EnvPath | Where-Object { $_ -match '^\s*REMOTE_' } | ForEach-Object {
    if ($_ -match '^\s*([^=]+)=(.*)$') {
        $env_vars[$matches[1]] = $matches[2]
    }
}

# Validate required vars
$required = @("REMOTE_HOST", "REMOTE_USER", "REMOTE_KEY_PATH", "REMOTE_PROJECT_ROOT")
foreach ($var in $required) {
    if (-not $env_vars.ContainsKey($var) -or [string]::IsNullOrWhiteSpace($env_vars[$var])) {
        Write-Host "ERROR: $var is not set in .env" -ForegroundColor Red
        exit 1
    }
}

$RemoteHost = $env_vars["REMOTE_HOST"]
$RemoteUser = $env_vars["REMOTE_USER"]
$RemoteKeyPath = $env_vars["REMOTE_KEY_PATH"] -replace '^~', $env:USERPROFILE
$RemoteProjectRoot = $env_vars["REMOTE_PROJECT_ROOT"]

# Validate SSH key exists
if (-not (Test-Path $RemoteKeyPath)) {
    Write-Host "ERROR: SSH key not found at $RemoteKeyPath" -ForegroundColor Red
    exit 1
}

# Local directories (created by scp if needed)
$LocalLogs = Join-Path $ProjectRoot "logs"
$LocalData = Join-Path $ProjectRoot "data"

Write-Host "Syncing remote data..." -ForegroundColor Yellow
Write-Host "  Remote host: $RemoteHost"
Write-Host "  User: $RemoteUser"
Write-Host "  SSH key: $RemoteKeyPath"
Write-Host "  Local project root: $ProjectRoot"
Write-Host ""

# Helper function to sync directory via scp
function Sync-Directory {
    param(
        [string]$RemoteDir,
        [string]$LocalDir,
        [string]$DirName
    )

    Write-Host "Syncing $DirName directory..." -ForegroundColor Yellow

    # Use scp to recursively copy files to parent directory
    # Note: scp doesn't have native delta-sync, but it's fast for incremental updates
    $RemotePath = "${RemoteUser}@${RemoteHost}:${RemoteProjectRoot}/${RemoteDir}"

    # scp -r (recursive) -i (identity file) -p (preserve time/permissions)
    # Copy to parent dir so scp creates the subdirectory correctly
    $scp_args = @(
        "-r",
        "-i", $RemoteKeyPath,
        "-o", "StrictHostKeyChecking=no",
        "$RemotePath",
        $ProjectRoot
    )

    try {
        & scp $scp_args 2>&1 | ForEach-Object { Write-Host $_ }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: Failed to sync $DirName directory" -ForegroundColor Red
            exit 1
        }
    } catch {
        Write-Host "ERROR: Failed to sync $DirName directory - $_" -ForegroundColor Red
        exit 1
    }
}

# Sync both directories
Sync-Directory "logs" $LocalLogs "logs"
Sync-Directory "data" $LocalData "data"

# Count files
$LogsCount = (Get-ChildItem -Path $LocalLogs -Recurse -File | Measure-Object).Count
$DataCount = (Get-ChildItem -Path $LocalData -Recurse -File | Measure-Object).Count

Write-Host "[OK] Data sync complete" -ForegroundColor Green
Write-Host ("  logs/: {0} files" -f $LogsCount)
Write-Host ("  data/: {0} files" -f $DataCount)
