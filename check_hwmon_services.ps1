# Check local daemon_writer + Flask dashboard (:8080).
# Default: if something is down, auto-start it in new PowerShell windows, wait, re-check.
#   .\check_hwmon_services.ps1
#   .\check_hwmon_services.ps1 -CheckOnly
param([switch]$CheckOnly)

$ErrorActionPreference = "Continue"
$RepoRoot = $PSScriptRoot
$DashDir = Join-Path $RepoRoot "windows_dashboard"
$DashboardProbeUrl = "http://127.0.0.1:8080/api/devices"
$MetricsPath = Join-Path $env:LOCALAPPDATA "hwmon_dashboard\metrics.jsonl"
$StaleSeconds = 45
# conda run + first collect() + 10s interval: 12s is often too short
$WaitPollSeconds = 3
$WaitMaxSeconds = 55

function Test-DashboardUp {
    try {
        $r = Invoke-WebRequest -Uri $DashboardProbeUrl -UseBasicParsing -TimeoutSec 8
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Test-WriterProcessRunning {
    $want = @("python.exe", "pythonw.exe", "conda.exe", "conda.bat")
    try {
        foreach ($p in Get-CimInstance Win32_Process -ErrorAction SilentlyContinue) {
            if ($want -notcontains ([string]$p.Name).ToLowerInvariant()) { continue }
            $cmd = [string]$p.CommandLine
            if ($cmd -match "daemon_writer") {
                return @{ Running = $true; ProcessId = $p.ProcessId }
            }
        }
    } catch {}
    return @{ Running = $false; ProcessId = $null }
}

function Get-JsonlWriteTimeUtc {
    if (-not (Test-Path -LiteralPath $MetricsPath)) { return $null }
    return (Get-Item -LiteralPath $MetricsPath).LastWriteTimeUtc
}

function Get-LocalMonitorStatus {
    $proc = Test-WriterProcessRunning
    if ($proc.Running) {
        return @{
            OK     = $true
            Detail = "daemon_writer process running (PID $($proc.ProcessId))"
        }
    }

    if (-not (Test-Path -LiteralPath $MetricsPath)) {
        return @{ OK = $false; Detail = "no metrics file and no daemon_writer process: $MetricsPath" }
    }

    $age = ((Get-Date) - (Get-Item -LiteralPath $MetricsPath).LastWriteTime).TotalSeconds
    if ($age -le $StaleSeconds) {
        return @{ OK = $true; Detail = "jsonl updated ~$([math]::Round($age))s ago" }
    }
    return @{
        OK     = $false
        Detail = "jsonl stale ~$([math]::Round($age))s (no daemon_writer process seen; threshold ${StaleSeconds}s)"
    }
}

$dashOk = Test-DashboardUp
$mon = Get-LocalMonitorStatus

Write-Host ""
Write-Host "=== HW monitor service check ===" -ForegroundColor Cyan
Write-Host ""

if ($dashOk) {
    Write-Host "Web dashboard: OK  $DashboardProbeUrl" -ForegroundColor Green
} else {
    Write-Host "Web dashboard: NOT running (http://127.0.0.1:8080)" -ForegroundColor Red
}
Write-Host "  Script: windows_dashboard\run_dashboard.ps1" -ForegroundColor DarkGray

Write-Host ""

if ($mon.OK) {
    Write-Host "Local writer:  $($mon.Detail)" -ForegroundColor Green
} else {
    Write-Host "Local writer:  $($mon.Detail)" -ForegroundColor Red
}
Write-Host "  Script: windows_dashboard\run_local_monitor.ps1" -ForegroundColor DarkGray

Write-Host ""

$startedSomething = $false
if (-not $CheckOnly) {
    $runDash = Join-Path $DashDir "run_dashboard.ps1"
    $runMon = Join-Path $DashDir "run_local_monitor.ps1"
    if (-not (Test-Path -LiteralPath $runDash)) {
        Write-Host "Missing run_dashboard.ps1 (run this script from repo root)." -ForegroundColor Yellow
    } else {
        $jsonlT0 = Get-JsonlWriteTimeUtc
        if (-not $dashOk) {
            Write-Host "Autostart: opening dashboard window..." -ForegroundColor Yellow
            Start-Process -FilePath "powershell.exe" -ArgumentList @(
                "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $runDash
            )
            $startedSomething = $true
        }
        if (-not $mon.OK) {
            Write-Host "Autostart: opening local monitor window..." -ForegroundColor Yellow
            Start-Process -FilePath "powershell.exe" -ArgumentList @(
                "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $runMon
            )
            $startedSomething = $true
        }
        if ($dashOk -and $mon.OK) {
            Write-Host "Both already running (no autostart needed)." -ForegroundColor DarkGray
        }
        if ($startedSomething) {
            Write-Host "Polling up to ${WaitMaxSeconds}s (conda + first sample can be slow)..." -ForegroundColor DarkGray
            $deadline = (Get-Date).AddSeconds($WaitMaxSeconds)
            while ((Get-Date) -lt $deadline) {
                $dashOk = Test-DashboardUp
                $mon = Get-LocalMonitorStatus
                if ($dashOk -and $mon.OK) { break }

                $t1 = Get-JsonlWriteTimeUtc
                if ($null -ne $t1 -and $null -ne $jsonlT0 -and $t1 -gt $jsonlT0) {
                    $mon = @{ OK = $true; Detail = "jsonl new write detected" }
                    if ($dashOk) { break }
                }
                if ($null -ne $t1 -and $null -eq $jsonlT0) {
                    $mon = @{ OK = $true; Detail = "jsonl created" }
                    if ($dashOk) { break }
                }

                Start-Sleep -Seconds $WaitPollSeconds
            }

            Write-Host ""
            Write-Host "After autostart:" -ForegroundColor Cyan
            if ($dashOk) {
                Write-Host "Web dashboard: OK" -ForegroundColor Green
            } else {
                Write-Host "Web dashboard: still not OK (see dashboard window for errors)" -ForegroundColor Red
            }
            if ($mon.OK) {
                Write-Host "Local writer:  $($mon.Detail)" -ForegroundColor Green
            } else {
                Write-Host "Local writer:  $($mon.Detail)" -ForegroundColor Red
                Write-Host "If a window opened for run_local_monitor.ps1, read the error there (conda env, psutil, etc.)." -ForegroundColor DarkGray
            }
            Write-Host ""
        }
    }
} else {
    Write-Host "CheckOnly: not starting any process." -ForegroundColor DarkGray
    Write-Host ""
}

$allOk = $dashOk -and $mon.OK
if (-not $allOk) {
    Write-Host "Result: FAIL (exit 1)" -ForegroundColor Yellow
} else {
    Write-Host "Result: OK (exit 0)" -ForegroundColor Green
}
Write-Host ""

if ($allOk) { exit 0 } else { exit 1 }
