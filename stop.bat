@echo off
rem Stops the Janome JP4 Control Panel started by run.bat.
rem English comments only

cd /d "%~dp0"

if "%JANOME_PORT%"=="" set "JANOME_PORT=8085"

echo [SYSTEM] Stopping Janome JP4 Control Panel...
taskkill /fi "WINDOWTITLE eq JANOME_JP4_PANEL*" /t /f >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr /r /c:":%JANOME_PORT% .*LISTENING"') do (
    taskkill /f /pid %%a >nul 2>&1
)

rem Remove stale runtime state so the next start does not see a dead reader process
if exist ".janome_jp4_process.pid" del ".janome_jp4_process.pid"
if exist ".janome_jp4_process.mode" del ".janome_jp4_process.mode"

echo [SYSTEM] Janome JP4 stopped.
