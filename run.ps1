# Launch the XEMM live dashboard (Windows / PowerShell).
#   ./run.ps1
# Stops with Ctrl-C.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$cfg  = Get-Content (Join-Path $PSScriptRoot "config.json") | ConvertFrom-Json
$url  = "http://$($cfg.bind_host):$($cfg.bind_port)"

# open the browser a moment after the server binds
Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 2; Start-Process $u } -ArgumentList $url | Out-Null

Write-Host "Starting XEMM dashboard at $url  (Ctrl-C to stop)"
python server.py
