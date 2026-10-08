@echo off
REM ============================================================
REM  ID Audio Sync - one-click build script (Windows)
REM  Output: dist\IDAudioSync.exe
REM  Run this file by double-clicking, or from a terminal.
REM ============================================================
setlocal
cd /d "%~dp0.."

echo [1/3] Locating Python...
set "PY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo   ERROR: Python not found. Install Python 3.9+ and retry.
  pause
  exit /b 1
)
echo   Using: %PY%

echo [2/3] Installing build dependencies...
%PY% -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo   pip install failed. Trying default index...
  %PY% -m pip install --disable-pip-version-check -q -i https://pypi.org/simple -r requirements.txt
)
if errorlevel 1 (
  echo   ERROR: dependency installation failed.
  pause
  exit /b 1
)

echo [3/3] Building with PyInstaller...
%PY% -m PyInstaller --clean --noconfirm build\idaudiosync.spec
if errorlevel 1 (
  echo   ERROR: build failed.
  pause
  exit /b 1
)

echo.
echo Done. Output: %CD%\dist\IDAudioSync.exe
echo Copy it to any Windows machine and double-click to install.
pause
endlocal
