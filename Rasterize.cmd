@echo off
setlocal
pushd "%~dp0"
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%CD%\rasterize_gui.ps1" %*
popd
endlocal
