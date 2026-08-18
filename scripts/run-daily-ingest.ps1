$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $uvCommand) {
    $uvPath = $uvCommand.Source
} else {
    $uvPath = Join-Path $env:LOCALAPPDATA "hermes\bin\uv.exe"
    if (-not (Test-Path -LiteralPath $uvPath)) {
        throw "No se encontró uv en PATH ni en $uvPath"
    }
}

$logDirectory = Join-Path $projectRoot "var\log"
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory "daily-ingest.log"
$runDate = Get-Date -Format "yyyy-MM-dd"

# Habilitación explícita y acotada al proceso autorizado por la tarea diaria.
$env:LIVE_SOURCE_ENABLED = "true"

Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format o)] Inicio de ingesta $runDate"
# El comando nativo corre con preferencia Continue: bajo Windows PowerShell 5.1,
# $ErrorActionPreference=Stop convierte cualquier línea de stderr redirigida con
# 2>&1 en error terminante, y el script moría sin registrar ni el motivo ni el
# "Fin" (corridas del 2026-08-14 al 17: código 0x1 y bitácora vacía).
$exitCode = 1
try {
    $ErrorActionPreference = "Continue"
    & $uvPath run kipu ingest-date --date $runDate 2>&1 |
        ForEach-Object { "$_" } |
        Tee-Object -FilePath $logPath -Append
    $exitCode = $LASTEXITCODE
} catch {
    Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format o)] ERROR: $_"
} finally {
    $ErrorActionPreference = "Stop"
}
Add-Content -LiteralPath $logPath -Value "[$(Get-Date -Format o)] Fin de ingesta $runDate (código $exitCode)"
exit $exitCode
