@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE="
set "BUNDLED_PY=%~dp0python\python.exe"
set "VENV_PY=%~dp0venv\Scripts\python.exe"

if exist "%BUNDLED_PY%" (
    "%BUNDLED_PY%" -c "import requests, fastapi, uvicorn, httpx, PIL, pydantic" >nul 2>&1
    if not errorlevel 1 set "PYEXE=%BUNDLED_PY%"
)

if not defined PYEXE if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import requests, fastapi, uvicorn, httpx, PIL, pydantic" >nul 2>&1
    if not errorlevel 1 set "PYEXE=%VENV_PY%"
)

if not defined PYEXE (
    python -c "import requests, fastapi, uvicorn, httpx, PIL, pydantic" >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    echo [错误] 未找到已安装项目依赖的 Python 环境。
    echo 请先运行：python -m venv venv
    echo 然后运行：.\venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo 正在启动 Infinite Canvas...
echo 使用 Python：%PYEXE%
echo 访问地址：http://127.0.0.1:3000/
echo 按 Ctrl+C 停止服务。
echo.

start /b cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:3000/"
"%PYEXE%" main.py

echo.
echo 服务已停止。
pause
