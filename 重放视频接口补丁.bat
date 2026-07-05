@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\patches\apply_video_request_mode_patch.ps1" %*
exit /b %ERRORLEVEL%
