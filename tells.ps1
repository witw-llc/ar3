$A8sPath = Join-Path $PSScriptRoot "apps/a8s/a8s.py"

$Python = Get-Command python3 -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source $A8sPath tells @args; exit $LASTEXITCODE }
$Python = Get-Command python -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source $A8sPath tells @args; exit $LASTEXITCODE }
$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source -3 $A8sPath tells @args; exit $LASTEXITCODE }

Write-Error "Could not find python3, python, or py on PATH."
exit 127
