# Append local Windows metrics to JSONL for dashboard device `local-win` (see devices.yaml).
# Run from repo: keep this window open while using the dashboard.
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$repoRoot = Split-Path $here
$envName = "hwmon-dashboard"
$jsonl = Join-Path $env:LOCALAPPDATA "hwmon_dashboard\metrics.jsonl"

if (-not (conda env list | Select-String -Pattern "\b$envName\b")) {
    Write-Host "请先创建环境: conda env create -f `"$here\environment.yml`""
    exit 1
}

Set-Location $repoRoot
Write-Host "Writing to $jsonl (10s interval). Ctrl+C to stop."
conda run --no-capture-output -n $envName python "$repoRoot\daemon_writer.py" --jsonl $jsonl
