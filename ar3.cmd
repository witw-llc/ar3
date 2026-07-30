@echo off
setlocal
set "BIN_DIR=%~dp0"
set "AR3=%BIN_DIR%apps\ar3\ar3.py"

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python "%AR3%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%AR3%" %*
    exit /b %ERRORLEVEL%
)

echo ar3: python not found on PATH >&2
exit /b 127
