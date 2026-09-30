@echo off
REM ============================================================
REM  FENTAY B2B Order Parser - Menu Launcher
REM  NOTE: This file must stay ASCII-only. Chinese characters
REM        in .bat files cause mojibake on Windows.
REM ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PY=python"
set "PDF="
for %%f in (*.pdf) do if not defined PDF set "PDF=%%f"

if not exist "FENTAY_B2B\SKILL.md" (
    echo [ERROR] FENTAY_B2B\SKILL.md not found.
    pause
    exit /b 1
)

:menu
cls
echo ==========================================================
echo   FENTAY B2B Order Parser
echo ==========================================================
echo   Work dir : %CD%
echo   PDF file : %PDF%
echo.
echo   1) Run unit tests            (fast, ~2 sec)
echo   2) Route C - text layer       (fast, ~1 sec, no LLM)
echo   3) Route B - qwen3.5:4b hybrid (recommended, ~2 min)
echo   4) Route B - full matrix      (2 models x 2 modes, ~20 min)
echo   5) Compare all existing results
echo   6) Check environment
echo   0) Exit
echo ==========================================================
echo.

if not defined PDF (
    echo [ERROR] No PDF file found in this directory.
    echo.
    set /p CH="Press Enter to return to menu..."
    goto menu
)

set /p CHOICE="Select [0-6]: "

if "%CHOICE%"=="1" goto run_tests
if "%CHOICE%"=="2" goto run_c
if "%CHOICE%"=="3" goto run_b_recommended
if "%CHOICE%"=="4" goto run_b_matrix
if "%CHOICE%"=="5" goto compare_all
if "%CHOICE%"=="6" goto check_env
if "%CHOICE%"=="0" goto done

echo.
echo [WARN] Invalid choice.
goto pause_menu

REM ------------------------------------------------------------
:run_tests
cls
echo [1/1] Running unit tests...
echo.
%PY% test_fentay.py
goto pause_menu

REM ------------------------------------------------------------
:run_c
cls
echo [1/1] Route C: parsing PDF text layer (no LLM)...
echo.
%PY% pdf_text_fentay.py --pdf "%PDF%" --compare "expected.json" --dump-columns
goto pause_menu

REM ------------------------------------------------------------
:run_b_recommended
cls
echo [1/1] Route B: qwen3.5:4b + hybrid mode...
echo      This takes about 2 minutes. Please wait.
echo.
%PY% ollama_fentay.py --pdf "%PDF%" --skill-dir "FENTAY_B2B" --model qwen3.5:4b --mode hybrid --out-base "result" --compare "expected.json"
goto pause_menu

REM ------------------------------------------------------------
:run_b_matrix
set /p CONFIRM="Full matrix takes about 20 minutes. Continue? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    goto pause_menu
)
cls
echo Running 4 combinations: 2 models x 2 modes...
echo This takes about 20 minutes. Please wait.
echo.
%PY% ollama_fentay.py --pdf "%PDF%" --skill-dir "FENTAY_B2B" --model qwen3.5:4b --model gemma4:12b --mode pure --mode hybrid --out-base "result" --compare "expected.json" --show-diffs 12
goto pause_menu

REM ------------------------------------------------------------
:compare_all
cls
echo Comparing all result files against expected.json...
echo.
%PY% compare_results.py
if errorlevel 1 (
    echo.
    echo [ERROR] compare_results.py failed.
)
goto pause_menu

REM ------------------------------------------------------------
:check_env
cls
echo Checking environment...
echo.
%PY% check_env.py
echo.
echo --- Ollama service ---
%PY% -c "import urllib.request,json;r=urllib.request.urlopen('http://localhost:11434/api/tags',timeout=5);d=json.load(r);print('  Connected. Models available:');[print('    '+m['name']) for m in d.get('models',[])]" 2>nul
if errorlevel 1 echo   [WARN] Ollama not reachable at localhost:11434
echo.
echo --- Folder contents ---
dir /b
goto pause_menu

REM ------------------------------------------------------------
:pause_menu
echo.
echo --------------------------------------------------
set /p DUMMY="Press Enter to return to menu..."
goto menu

:done
endlocal
exit /b 0
