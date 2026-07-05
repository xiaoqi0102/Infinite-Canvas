@echo off
cd /d "%~dp0"

set "PYEXE=%~dp0python\python.exe"
if exist "%PYEXE%" (
    "%PYEXE%" -c "import requests, fastapi, uvicorn, httpx, PIL, pydantic" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] Bundled Python dependencies are incomplete, using system Python...
        set "PYEXE=python"
    )
) else (
    set "PYEXE=python"
)

echo Starting ComfyUI-API-Modelscope...
echo Visit: http://127.0.0.1:3000/
echo Press Ctrl+C to stop.
echo.

start /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:3000/"
"%PYEXE%" main.py

echo.
echo Server stopped.
pause
