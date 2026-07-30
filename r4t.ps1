$R4tPath = Join-Path $PSScriptRoot "apps/r4t/r4t.py"

$Python = Get-Command python3 -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source $R4tPath @args; exit $LASTEXITCODE }
$Python = Get-Command python -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source $R4tPath @args; exit $LASTEXITCODE }
$Python = Get-Command py -ErrorAction SilentlyContinue
if ($Python) { & $Python.Source -3 $R4tPath @args; exit $LASTEXITCODE }

Write-Error "Could not find python3, python, or py on PATH."
exit 127
