Janome JP4 - installing on a PC next to the press (Windows 10/11, 64-bit)
=========================================================================

No Python and no other software is needed. Only the driver of the USB/RS-232 adapter.

1. Copy this whole folder (JanomeJP4) to the PC, to a place the program may write to, for example
   C:\JanomeJP4.  Not into "Program Files". Keep the "_internal" folder next to JanomeJP4.exe.
2. Install the driver of the USB/RS-232 adapter and find its port (COMx) in Device Manager.
3. Double-click  install.bat  and click "Yes" on the administrator permission window.
   It makes the panel start by itself when the computer starts (also after a power cut), starts it now,
   puts a shortcut "Janome JP4 Panel" on the desktop and opens http://localhost:8085.
4. In the panel, Settings: the serial port (COMx), the database address and token, the CSV folder.
   Press Save, then Start.

Everyday use
  - Panel:            http://localhost:8085   (from another PC: http://<this PC's address>:8085, allow TCP 8085 in the firewall)
  - Start / stop by hand:  run.bat / stop.bat   (no window opens; the log is logs\panel.log)
  - Event log:        the "Event log" card in the panel, or logs\events.log
  - Test the COM port without the panel:  JanomeJP4.exe --diagnostic   then open http://localhost:8086
  - Change "start with Windows" later:    Settings > Startup > Start with Windows
  - Remove:           uninstall.bat, then delete the folder

Without the installer: run.bat starts the panel for this session only.

config.json in this folder holds the settings, including the database token: keep the folder private.
