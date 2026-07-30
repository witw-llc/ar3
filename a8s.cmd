@echo off
setlocal
set "BIN_DIR=%~dp0"
set "A8S=%BIN_DIR%apps\a8s\a8s.py"

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python "%A8S%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%A8S%" %*
    exit /b %ERRORLEVEL%
)

echo a8s: python not found on PATH >&2
exit /b 127
