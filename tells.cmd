@echo off
setlocal
rem No delayed expansion in this file, ever. cmd substitutes %* into the
rem line first and expands `!` in the result second, so with delayed
rem expansion on, an exclamation mark in a user argument is silently
rem eaten -- `tell bob "ship!"` loses the bang, and a body containing
rem !PATH! becomes environment data.
rem
rem `exit /b %ERRORLEVEL%` is correct only OUTSIDE a parenthesised block:
rem cmd expands %VAR% for a whole block at parse time, so inside one it
rem returns the value from before the block ran. The labels below keep
rem every read on its own line, which is why there are no blocks here.
set "BIN_DIR=%~dp0"
set "A8S=%BIN_DIR%apps\a8s\a8s.py"

rem A stock Windows console is cp1252 and the suite's own output --
rem arrows, em dashes, agent names -- dies on encode there. UTF-8 mode
rem fixes it in the child rather than asking every user to change a code
rem page. A caller that set either variable has already chosen.
if not defined PYTHONUTF8 if not defined PYTHONIOENCODING set "PYTHONUTF8=1"

rem AR3_PYTHON names an interpreter outright and is tried first, so a
rem harness that bundles its own python can be pointed at without editing
rem PATH. It is probed like every other candidate: a path that does not
rem run is a typo, and obeying one silently would trade a working PATH
rem for nothing.
if not defined AR3_PYTHON goto :probe_path
"%AR3_PYTHON%" -c "pass" >nul 2>&1
if not errorlevel 1 goto :use_ar3_python
echo tells: AR3_PYTHON=%AR3_PYTHON% does not run; falling back to PATH >&2

:probe_path
rem Each candidate has to RUN before it is believed. On Windows the first
rem `python` on PATH is often the Microsoft Store alias, which resolves
rem and then exits without running anything, so `where` is not acceptance.
python3 -c "pass" >nul 2>&1
if not errorlevel 1 goto :use_python3
python -c "pass" >nul 2>&1
if not errorlevel 1 goto :use_python
py -3 -c "pass" >nul 2>&1
if not errorlevel 1 goto :use_py
echo tells: no working python3, python, or py -3 on PATH >&2
exit /b 127

:use_ar3_python
"%AR3_PYTHON%" "%A8S%" tells %*
exit /b %ERRORLEVEL%

:use_python3
python3 "%A8S%" tells %*
exit /b %ERRORLEVEL%

:use_python
python "%A8S%" tells %*
exit /b %ERRORLEVEL%

:use_py
py -3 "%A8S%" tells %*
exit /b %ERRORLEVEL%
