$K7ePath = Join-Path $PSScriptRoot "apps/k7e/k7e.py"

# A stock Windows console is cp1252, and the suite's own output — arrows, em
# dashes, agent names — dies on encode there. UTF-8 mode fixes it in the child
# rather than asking every user to change a code page. A caller that set
# either variable has already chosen, and that choice wins.
if (-not $env:PYTHONUTF8 -and -not $env:PYTHONIOENCODING) { $env:PYTHONUTF8 = "1" }

# python3, then python, then py -3 — the same order the bash half resolves
# in. A candidate has to RUN before it is accepted, not merely be found: on
# Windows the first `python3` on PATH is often the Microsoft Store alias, a
# stub that opens the Store and exits non-zero.
$PythonExe = $null
$PythonArgs = @()
# AR3_PYTHON names an interpreter outright and is tried first, so a harness
# that bundles its own python can be pointed at without editing PATH. It is
# probed like every other candidate: a path that does not run is a typo, and
# obeying one silently would trade a working PATH for nothing.
$Candidates = @()
if ($env:AR3_PYTHON) {
    $Candidates += @{ Name = $env:AR3_PYTHON; Args = @(); Override = $true }
}
$Candidates += @(
    @{ Name = "python3"; Args = @() },
    @{ Name = "python";  Args = @() },
    @{ Name = "py";      Args = @("-3") }
)
foreach ($Candidate in $Candidates) {
    $Found = Get-Command $Candidate.Name -ErrorAction SilentlyContinue
    if ($Found) {
        $Probe = $Candidate.Args
        # `-c "pass"`, never `-c ""`. Windows PowerShell 5.1 — the `powershell`
        # on a stock box — DROPS an empty-string argument to a native command,
        # so the interpreter sees a bare `-c`, answers "Argument expected for
        # the -c option" and exits 2. Every candidate would be rejected and
        # every command would exit 127, on the default shell, whatever the
        # PATH holds.
        try { & $Found.Source @Probe -c "pass" 2>$null } catch { $global:LASTEXITCODE = 1 }
        if ($LASTEXITCODE -eq 0) {
            $PythonExe = $Found.Source
            $PythonArgs = $Candidate.Args
            break
        }
    }
    if ($Candidate.Override) {
        [Console]::Error.WriteLine("k7e: AR3_PYTHON=$($Candidate.Name) does not run; falling back to PATH")
    }
}
if (-not $PythonExe) {
    [Console]::Error.WriteLine("k7e: no working python3, python, or py on PATH")
    exit 127
}
# Pipeline input reaches a script as the $input enumerator. PowerShell does
# not attach it to a native command the script starts, so a here-string piped
# here handed python an empty stdin and `tell <name> -` staged a blank body.
# Piping $input unconditionally is not the fix either: an empty $input is an
# immediate EOF, which takes the console away from an interactive `-`.
if ($MyInvocation.ExpectingInput) {
    # What a script writes to a native command is encoded in $OutputEncoding,
    # which is ASCII on Windows PowerShell 5.1. That encoder turns an em dash
    # or a CJK run into `?` before python is started, so PYTHONUTF8 in the
    # child cannot bring it back. UTF-8 without a BOM encodes every character,
    # and the assignment is script-scoped, so the caller's session is left as
    # it was.
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $input | & $PythonExe @PythonArgs $K7ePath @args
} else {
    & $PythonExe @PythonArgs $K7ePath @args
}
exit $LASTEXITCODE
