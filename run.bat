@echo off
REM ============================================================
REM  FENTAY B2B Order Parser - launcher
REM
REM  NOTE: This file must stay ASCII-only. Chinese characters
REM        in .bat files cause mojibake on Windows.
REM
REM  The menu lives in menu.py so that adding a route only
REM  requires editing one place.
REM ============================================================
setlocal
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PY=python"
if defined PYTHON set "PY=%PYTHON%"

%PY% menu.py
if errorlevel 1 (
    echo.
    echo [ERROR] menu.py exited with an error. See messages above.
    pause
)

endlocal
exit /b 0