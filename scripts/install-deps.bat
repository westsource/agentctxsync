@echo off
rem hermes-sync client dependency bootstrap (Windows)
rem Creates <extract-dir>\venv with mcp (and zstandard for dsh) and prints
rem the interpreter to register as <PYTHON>. Run once after unzipping.
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "VENV=%ROOT%venv"

if exist "%VENV%\Scripts\python.exe" goto :done

where python >nul 2>nul
if %errorlevel%==0 (
  set "PY=python"
) else (
  py -3 -c "import sys" >nul 2>nul
  if %errorlevel%==0 (set "PY=py -3") else (
    echo [hermes-sync] ERROR: no Python 3 found on PATH ^(install python.org ^3.10+^)
    exit /b 1
  )
)

echo [hermes-sync] creating venv at "%VENV%" ...
"%PY%" -m venv "%VENV%"
if errorlevel 1 exit /b 1
echo [hermes-sync] installing mcp + zstandard (first run needs network) ...
"%VENV%\Scripts\python.exe" -m pip install --quiet mcp zstandard
if errorlevel 1 (
  echo [hermes-sync] ERROR: pip install failed - check network/proxy
  exit /b 1
)

:done
echo [hermes-sync] ready. Register this as ^<PYTHON^>:
echo   "%VENV%\Scripts\python.exe"
echo   args: "%ROOT%mcp\server.py"
exit /b 0
