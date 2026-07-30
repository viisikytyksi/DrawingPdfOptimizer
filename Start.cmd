@echo off
setlocal
set "APP_DIR=%~dp0"
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%gui.ps1" %*
endlocal
