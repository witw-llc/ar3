$Ar3Path = Join-Path $PSScriptRoot "apps/ar3/ar3.py"

$Python = Get-Command python3 -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source $Ar3Path @args; exit $LASTEXITCODE }
$Python = Get-Command python -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source $Ar3Path @args; exit $LASTEXITCODE }
$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source -3 $Ar3Path @args; exit $LASTEXITCODE }

Write-Error "Could not find python3, python, or py on PATH."
exit 127
