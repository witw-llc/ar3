$A8sPath = Join-Path $PSScriptRoot "apps/a8s/a8s.py"

# python3, then python, then py -3 — the same order the bash half resolves
# in. A candidate has to RUN before it is accepted, not merely be found: on
# Windows the first `python3` on PATH is often the Microsoft Store alias, a
# stub that opens the Store and exits non-zero.
$PythonExe = $null
$PythonArgs = @()
foreach ($Candidate in @(
    @{ Name = "python3"; Args = @() },
    @{ Name = "python";  Args = @() },
    @{ Name = "py";      Args = @("-3") }
)) {
    $Found = Get-Command $Candidate.Name -ErrorAction SilentlyContinue
    if (-not $Found) { continue }
    $Probe = $Candidate.Args
    # `-c "pass"`, never `-c ""`. Windows PowerShell 5.1 — the `powershell` on
    # a stock box — DROPS an empty-string argument to a native command, so the
    # interpreter sees a bare `-c`, answers "Argument expected for the -c
    # option" and exits 2. Every candidate would be rejected and every command
    # would exit 127, on the default shell, whatever the PATH holds.
    try { & $Found.Source @Probe -c "pass" 2>$null } catch { continue }
    if ($LASTEXITCODE -eq 0) {
        $PythonExe = $Found.Source
        $PythonArgs = $Candidate.Args
        break
    }
}
if (-not $PythonExe) {
    [Console]::Error.WriteLine("tell: no working python3, python, or py on PATH")
    exit 127
}
& $PythonExe @PythonArgs $A8sPath tell @args
exit $LASTEXITCODE
