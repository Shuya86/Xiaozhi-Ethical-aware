@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Xiaozhi AI Backend - One-Click Startup
color 0A

if /I "%~1"=="--help" goto usage
if /I "%~1"=="/?" goto usage

set "NO_PAUSE=0"
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"

set "PYTHON_EXE="
set "PYTHON_ARGS="

if exist "%USERPROFILE%\anaconda3\python.exe" (
  set "PYTHON_EXE=%USERPROFILE%\anaconda3\python.exe"
  goto python_found
)

if exist "%USERPROFILE%\miniconda3\python.exe" (
  set "PYTHON_EXE=%USERPROFILE%\miniconda3\python.exe"
  goto python_found
)

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_EXE=py"
  set "PYTHON_ARGS=-3"
  goto python_found
)

where python >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_EXE=python"
  goto python_found
)

echo [ERROR] Python 3 or Miniconda was not found.
echo Run deploy.bat for first-time installation and deployment.
if "%NO_PAUSE%"=="0" pause
exit /b 1

:python_found
echo =============================================================
echo   Xiaozhi AI backend daily startup
echo =============================================================
echo.
echo Repository: %CD%
echo.

if not exist "auto_deploy.py" (
  echo [ERROR] auto_deploy.py is missing from %CD%
  if "%NO_PAUSE%"=="0" pause
  exit /b 1
)

echo [1/3] Starting prerequisites, databases, and core services...
"%PYTHON_EXE%" %PYTHON_ARGS% auto_deploy.py --only prereqs,db,api,web,server --no-install
if errorlevel 1 goto core_failed

echo.
echo [2/3] Starting installed optional services...
set "OPTIONAL_FOUND=0"

if exist "%~dp0..\voiceprint-api\" (
  set "OPTIONAL_FOUND=1"
  "%PYTHON_EXE%" %PYTHON_ARGS% auto_deploy.py --only voiceprint
  if errorlevel 1 echo [WARN] voiceprint-api did not start; the core system remains available.
)

if exist "%~dp0..\mcp-endpoint-server\" (
  set "OPTIONAL_FOUND=1"
  "%PYTHON_EXE%" %PYTHON_ARGS% auto_deploy.py --only mcp
  if errorlevel 1 echo [WARN] mcp-endpoint-server did not start; the core system remains available.
)

if "%OPTIONAL_FOUND%"=="0" echo No optional service repository was found; skipping.

echo.
echo [3/3] Checking final service status...
"%PYTHON_EXE%" %PYTHON_ARGS% auto_deploy.py --status
if errorlevel 1 goto status_failed

echo.
echo =============================================================
echo   Backend startup completed.
echo   Admin console : http://127.0.0.1:8001
echo   Text chat     : http://127.0.0.1:8003/text_chat
echo.
echo   Connect the ESP32 first before using browser text chat.
echo =============================================================
if "%NO_PAUSE%"=="0" pause
exit /b 0

:core_failed
echo.
echo [ERROR] Core startup failed.
echo Check the error above and the logs listed in main\README_DEPLOY_ZH.md.
echo For first-time deployment, run deploy.bat instead.
if "%NO_PAUSE%"=="0" pause
exit /b 1

:status_failed
echo.
echo [ERROR] Services were started, but the final status check failed.
if "%NO_PAUSE%"=="0" pause
exit /b 1

:usage
echo Usage: run_xiaozhi-server.bat [--no-pause]
echo.
echo Daily launcher for an already deployed Xiaozhi backend.
echo It starts Docker Desktop when needed, MySQL, Redis, manager-api,
echo manager-web, xiaozhi-server, and installed optional services.
echo --no-pause exits immediately after completion for scripts or testing.
echo.
echo Use deploy.bat for the first deployment or dependency installation.
exit /b 0
