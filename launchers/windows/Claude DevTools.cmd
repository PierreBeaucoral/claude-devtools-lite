@echo off
REM Claude DevTools — double-click launcher (Windows).
REM Runs the PowerShell launcher without leaving a console window open.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ^
  -File "%~dp0claude-devtools.ps1"
exit
