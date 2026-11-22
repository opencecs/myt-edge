@echo off
setlocal
set "PSModulePath="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Flash-Debian.ps1" %*
exit /b %errorlevel%
