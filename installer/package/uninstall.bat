@echo off
rem Removes "start with the computer" and the desktop shortcut, and stops the panel. The folder and its data stay.
rem English comments only

cd /d "%~dp0"

net session >nul 2>&1
if %errorlevel% equ 0 goto admin

echo Asking for the administrator permission...
powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b

:admin
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_autostart.ps1"
powershell -NoProfile -Command "Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'Janome JP4 Panel.url')"
call "%~dp0stop.bat"
echo.
echo Done. To delete the program, delete this folder (the data and logs in it go with it).
pause
