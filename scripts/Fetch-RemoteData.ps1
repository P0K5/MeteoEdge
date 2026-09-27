#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Incrementally sync logs/ and data/ from the production server over SSH.
    Uses native Windows OpenSSH (ssh/scp) plus Python -- no external dependencies.

.DESCRIPTION
    Reads REMOTE_* credentials from .env and transfers only what changed:

      * files already held identically are not re-downloaded at all;
      * append-only logs resume from where the local copy ends;
      * SQLite databases arrive as consistent server-side snapshots (SQLite's
        online backup API), never as a byte copy of a file the bot is writing,
        and only the 4 MiB blocks that differ from the local copy are sent.

    Every transfer is digest-verified before it replaces a local file, so a
    torn or truncated copy can no longer land in data/.

    Sync state is remembered in .remote-sync-state.json at the repo root;
    delete it (or pass -Full) to force a complete, verified refresh.

    Exits 1 on failure (unreachable host, missing credentials, failed
    verification) -- see .claude/skills/health-triage/SKILL.md for how an agent
    session should treat that.

.PARAMETER Full
    Ignore remembered state and re-fetch everything.

.PARAMETER DryRun
    Report what would be transferred, then stop.

.PARAMETER SkipDatabases
    Sync logs/ and other plain files only -- the fast path when you just want
    fresh logs.

.PARAMETER DatabasesOnly
    Sync data/*.db only.

.PARAMETER NoDelta
    Always transfer whole database snapshots instead of block deltas.

.PARAMETER Exclude
    Glob(s) to leave out, e.g. -Exclude 'cryptoedge.db','*.bak*'. Can also be
    set persistently as REMOTE_SYNC_EXCLUDE in .env (comma-separated).

.PARAMETER Only
    Glob(s) to restrict the sync to, e.g. -Only 'data/meteoedge.db','logs/bot.log'.

.EXAMPLE
    powershell -File scripts\Fetch-RemoteData.ps1
    powershell -File scripts\Fetch-RemoteData.ps1 -DryRun
    powershell -File scripts\Fetch-RemoteData.ps1 -Only 'data/meteoedge.db','logs/bot.log'
#>

param(
    [string]$EnvFile = ".env",
    [switch]$Full,
    [switch]$DryRun,
    [switch]$SkipDatabases,
    [switch]$DatabasesOnly,
    [switch]$NoDelta,
    [string[]]$Exclude,
    [string[]]$Only,
    [int]$BlockSizeMB = 4,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

# Project root (this script lives in scripts/)
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Driver = Join-Path $PSScriptRoot "remote_sync.py"

if (-not (Test-Path $Driver)) {
    Write-Host "ERROR: sync driver not found at $Driver" -ForegroundColor Red
    exit 1
}

# Locate an interpreter: the repo venv first, then anything on PATH. The driver
# needs nothing but the standard library.
function Resolve-Python {
    if ($env:METEOEDGE_PYTHON -and (Test-Path $env:METEOEDGE_PYTHON)) {
        return @($env:METEOEDGE_PYTHON)
    }
    foreach ($rel in @(".venv-win\Scripts\python.exe", ".venv\Scripts\python.exe",
                       "venv\Scripts\python.exe")) {
        $candidate = Join-Path $ProjectRoot $rel
        if (Test-Path $candidate) { return @($candidate) }
    }
    foreach ($name in @("python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return @($cmd.Source) }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @($py.Source, "-3") }
    return $null
}

# @() guards the single-element case: PowerShell unwraps a one-item array.
$Python = @(Resolve-Python)
if (-not $Python) {
    Write-Host "ERROR: no Python interpreter found." -ForegroundColor Red
    Write-Host "       Expected $ProjectRoot\.venv-win\Scripts\python.exe, or python on PATH." -ForegroundColor Red
    exit 1
}

# Build driver arguments
$DriverArgs = @($Driver, "--env-file", $EnvFile, "--project-root", $ProjectRoot,
                "--block-size-mb", $BlockSizeMB)
if ($Full)          { $DriverArgs += "--full" }
if ($DryRun)        { $DriverArgs += "--dry-run" }
if ($SkipDatabases) { $DriverArgs += "--skip-databases" }
if ($DatabasesOnly) { $DriverArgs += "--databases-only" }
if ($NoDelta)       { $DriverArgs += "--no-delta" }
if ($Quiet)         { $DriverArgs += "--quiet" }
foreach ($pattern in $Exclude) { $DriverArgs += @("--exclude", $pattern) }
foreach ($pattern in $Only)    { $DriverArgs += @("--only", $pattern) }

# Unbuffered so progress appears as it happens rather than at the end.
$env:PYTHONUNBUFFERED = "1"

$Invoke = @($Python) + @($DriverArgs)
& $Invoke[0] $Invoke[1..($Invoke.Length - 1)]
exit $LASTEXITCODE
