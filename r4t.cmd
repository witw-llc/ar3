@echo off
setlocal
set "BIN_DIR=%~dp0"
set "R4T=%BIN_DIR%apps\r4t\r4t.py"

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python "%R4T%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%R4T%" %*
    exit /b %ERRORLEVEL%
)

echo r4t: python not found on PATH >&2
exit /b 127
