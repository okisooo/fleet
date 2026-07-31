@echo off
REM fleet tray widget - single hidden process from dedicated venv. Right-click icon -> Quit.
set PYTHONUTF8=1
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0tray.py"
