@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ai.ps1" launch
if errorlevel 1 (
  echo.
  echo AI-Stack did not finish starting. Review the message above and try again.
  pause
  exit /b 1
)
endlocal
