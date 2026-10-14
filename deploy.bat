@echo off
setlocal
cd /d "%~dp0"

if exist "%USERPROFILE%\anaconda3\python.exe" (
  "%USERPROFILE%\anaconda3\python.exe" deploy_gui.py
  if not errorlevel 1 exit /b 0
)

if exist "%USERPROFILE%\miniconda3\python.exe" (
  "%USERPROFILE%\miniconda3\python.exe" deploy_gui.py
  if not errorlevel 1 exit /b 0
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 deploy_gui.py
  if not errorlevel 1 exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
  python deploy_gui.py
  if not errorlevel 1 exit /b 0
)

echo.
echo Unable to start the deployment GUI. Install Python 3.10+ or Miniconda,
echo then run this file again.
pause
exit /b 1
