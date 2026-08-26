[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$skillRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $skillRoot '.venv'
$requirements = Join-Path $skillRoot 'requirements.txt'

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw 'Python 3.11 or later is required.'
}
$pythonVersion = & $pythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$versionParts = $pythonVersion.Split('.')
if ([int]$versionParts[0] -lt 3 -or ([int]$versionParts[0] -eq 3 -and [int]$versionParts[1] -lt 11)) {
    throw "Python 3.11 or later is required; found $pythonVersion."
}

if (-not (Test-Path -LiteralPath $runtimeRoot)) {
    & $pythonCommand.Source -m venv $runtimeRoot
}
$runtimePython = Join-Path $runtimeRoot 'Scripts\python.exe'
& $runtimePython -m pip install --disable-pip-version-check --requirement $requirements

Write-Host 'DWG conversion is optional and uses an external converter.'
Write-Host 'Install and license ODA File Converter, AutoCAD Core Console, or dwg2dxf yourself.'
Write-Host 'Install 7-Zip yourself if you need RAR or 7z extraction; no third-party license is accepted here.'

& $runtimePython (Join-Path $PSScriptRoot 'cad_quote.py') doctor
exit $LASTEXITCODE
