[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$skillRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $skillRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Skill runtime is not installed. Run scripts\setup.ps1 first."
}

& $python (Join-Path $PSScriptRoot 'cad_quote.py') @Arguments
exit $LASTEXITCODE
