@echo off
cd /d "%~dp0"

set "PYEXE=%~dp0python\python.exe"
if not exist "%PYEXE%" goto try_venv
"%PYEXE%" -c "import requests, fastapi, uvicorn, httpx, PIL, pydantic" >nul 2>&1
if errorlevel 1 goto try_venv
goto python_ready

:try_venv
set "PYEXE=%~dp0venv\Scripts\python.exe"
if not exist "%PYEXE%" goto try_system
"%PYEXE%" -c "import requests, fastapi, uvicorn, httpx, PIL, pydantic" >nul 2>&1
if errorlevel 1 goto try_system
goto python_ready

:try_system
set "PYEXE=python"
python -c "import requests, fastapi, uvicorn, httpx, PIL, pydantic" >nul 2>&1
if errorlevel 1 goto no_python

:python_ready
if /i "%~1"=="--check" goto check_only

echo Starting Infinite Canvas...
echo Python: %PYEXE%
echo URL: http://127.0.0.1:3000/
echo Press Ctrl+C to stop.
echo.

start /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:3000/"
"%PYEXE%" main.py

echo.
echo Server stopped.
pause
exit /b %ERRORLEVEL%

:check_only
echo PYEXE=%PYEXE%
exit /b 0

:no_python
echo [ERROR] No Python environment with the required dependencies was found.
echo Run: python -m venv venv
echo Then: .\venv\Scripts\python.exe -m pip install -r requirements.txt
pause
exit /b 1
