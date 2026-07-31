@echo off
setlocal
set "BIN_DIR=%~dp0"
set "K7E=%BIN_DIR%apps\k7e\k7e.py"

where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python "%K7E%" %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 "%K7E%" %*
    exit /b %ERRORLEVEL%
)

echo k7e: python not found on PATH >&2
exit /b 127
