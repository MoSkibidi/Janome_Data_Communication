@echo off
rem One-step installer. Double-click it: it asks for the administrator permission once, makes the panel
rem start by itself every time the computer starts, starts it now, puts a shortcut on the desktop
rem and opens the panel in the browser.
rem English comments only

cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% equ 0 goto admin

echo Asking for the administrator permission...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:admin
if "%JANOME_PORT%"=="" set "JANOME_PORT=8085"

echo [1/3] Setting the panel to start when the computer starts...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -Mode Startup -Start
if %errorlevel% neq 0 goto failed

echo [2/3] Putting a shortcut on the desktop...
powershell -NoProfile -Command "Set-Content -Path (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'Janome JP4 Panel.url') -Value '[InternetShortcut]','URL=http://localhost:%JANOME_PORT%'"

echo [3/3] Opening the panel...
timeout /t 8 /nobreak >nul
start "" "http://localhost:%JANOME_PORT%"

echo.
echo Done. The panel runs in the background and comes back by itself after a power cut or a restart.
echo Settings, the Event log and Start/Stop are in the panel: http://localhost:%JANOME_PORT%
pause
exit /b 0

:failed
echo.
echo The installation failed. Read the message above, then run install.bat again.
pause
exit /b 1
