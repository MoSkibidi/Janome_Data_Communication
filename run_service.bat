@echo off
rem Runs the Janome JP4 Control Panel in the foreground (no new window). Used by the Windows
rem scheduled task created with deploy\install_autostart.ps1, which restarts it if it ever exits.
rem Works for the packaged program (JanomeJP4.exe next to this file) and for the source tree (python app.py).
rem English comments only

cd /d "%~dp0"

if "%JANOME_PORT%"=="" set "JANOME_PORT=8085"

if not exist "logs" mkdir "logs"

set "PANEL_CMD=.\JanomeJP4.exe"
if exist "JanomeJP4.exe" goto run

set "PYTHON_BIN=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_BIN=.venv\Scripts\python.exe"
if not exist ".venv\Scripts\python.exe" if exist "venv\Scripts\python.exe" set "PYTHON_BIN=venv\Scripts\python.exe"
set "PANEL_CMD=%PYTHON_BIN% app.py"

:run
echo [%date% %time%] Starting Janome JP4 Control Panel >> logs\panel.log
%PANEL_CMD% >> logs\panel.log 2>&1
echo [%date% %time%] Panel exited with code %errorlevel% >> logs\panel.log
exit /b %errorlevel%
