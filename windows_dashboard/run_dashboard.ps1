# Start Flask dashboard (http://127.0.0.1:8080)
# Prereq: conda env created from environment.yml in this folder.
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
Set-Location $here

$envName = "hwmon-dashboard"
if (-not (conda env list | Select-String -Pattern "\b$envName\b")) {
    Write-Host "Creating conda env '$envName' from environment.yml ..."
    conda env create -f "$here\environment.yml"
}

Write-Host "Starting dashboard (conda run -n $envName) ..."
conda run --no-capture-output -n $envName python "$here\app.py"
