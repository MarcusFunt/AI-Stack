@echo off
setlocal
cd /d D:\AI-Stack
powershell.exe -NoProfile -WindowStyle Hidden -Command "if (-not (Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue)) { Start-Process -WindowStyle Hidden -FilePath 'C:\Users\marcu\AppData\Local\Programs\Python\Python311\pythonw.exe' -ArgumentList 'D:\AI-Stack\scripts\host_agent.py' -WorkingDirectory 'D:\AI-Stack' }"
endlocal
