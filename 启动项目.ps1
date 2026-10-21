param([int]$Port = 8766, [switch]$NoBrowser)
& (Join-Path $PSScriptRoot 'scripts\start.ps1') -Port $Port -NoBrowser:$NoBrowser
