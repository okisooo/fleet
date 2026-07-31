@echo off
REM Live unified agent-usage TUI. Ctrl+C to quit.
set PYTHONUTF8=1
uv run --with rich python "%~dp0fleet.py" %*
