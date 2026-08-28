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
set "K7E=%BIN_DIR%apps\k7e\k7e.py"

rem Each candidate has to RUN before it is believed. On Windows the first
rem `python` on PATH is often the Microsoft Store alias, which resolves
rem and then exits without running anything, so `where` is not acceptance.
python3 -c "pass" >nul 2>&1
if not errorlevel 1 goto :use_python3
python -c "pass" >nul 2>&1
if not errorlevel 1 goto :use_python
py -3 -c "pass" >nul 2>&1
if not errorlevel 1 goto :use_py
echo k7e: no working python3, python, or py -3 on PATH >&2
exit /b 127

:use_python3
python3 "%K7E%" %*
exit /b %ERRORLEVEL%

:use_python
python "%K7E%" %*
exit /b %ERRORLEVEL%

:use_py
py -3 "%K7E%" %*
exit /b %ERRORLEVEL%
