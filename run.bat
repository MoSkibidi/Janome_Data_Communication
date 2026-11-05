@echo off
rem Starts the Janome JP4 Control Panel in the background, with no window (default port 8085).
rem English comments only

cd /d "%~dp0"

if "%JANOME_PORT%"=="" set "JANOME_PORT=8085"

rem Safeguard check to prevent starting a duplicate instance
netstat -aon 2>nul | findstr /r /c:":%JANOME_PORT% .*LISTENING" >nul
if %errorlevel% equ 0 (
    echo [SYSTEM] Port %JANOME_PORT% is already in use. Janome JP4 may already be running.
    echo [SYSTEM] Run stop.bat first if you want to restart it.
    exit /b 1
)

rem Resolve Python binary (prefer virtualenv python over global python)
set "PYTHON_BIN="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_BIN=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON_BIN=venv\Scripts\python.exe"
) else (
    set "PYTHON_BIN=python"
)
if "%PYTHON_BIN%"=="python" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python was not found. Create a virtualenv and run: pip install -r requirements.txt
        exit /b 1
    )
)

if not exist "logs" mkdir "logs"

echo [SYSTEM] Using Python interpreter: %PYTHON_BIN%
echo [SYSTEM] Starting Janome JP4 Control Panel on port %JANOME_PORT% (all interfaces)...
rem The panel runs in a hidden window: nothing to close by mistake. Set JANOME_SHOW_WINDOW=1 to see it (minimized) while debugging.
if "%JANOME_SHOW_WINDOW%"=="1" goto show_window
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath cmd.exe -ArgumentList '/c title JANOME_JP4_PANEL && %PYTHON_BIN% app.py >> logs\panel.log 2>&1' -WorkingDirectory '%CD%' -WindowStyle Hidden"
goto started
:show_window
start "JANOME_JP4_PANEL" /min cmd /c "title JANOME_JP4_PANEL && %PYTHON_BIN% app.py >> logs\panel.log 2>&1"
:started

echo [SYSTEM] Accessible at http://localhost:%JANOME_PORT%
echo [SYSTEM] Logs: logs\panel.log
