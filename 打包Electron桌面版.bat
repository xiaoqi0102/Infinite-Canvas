@echo off
cd /d "%~dp0"

echo ============================================
echo   Build Infinite Canvas Electron Desktop
echo ============================================
echo.

python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

if not exist node_modules (
    echo [INFO] Installing Electron dependencies...
    npm install
    if errorlevel 1 (
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
)

echo.
set "BUILD_VERSION="
for /f "usebackq delims=" %%v in ("VERSION") do if not defined BUILD_VERSION set "BUILD_VERSION=%%v"
if not defined BUILD_VERSION (
    echo [ERROR] VERSION is empty or missing.
    pause
    exit /b 1
)
echo [INFO] Project VERSION: %BUILD_VERSION%
echo [INFO] Installer name will use this suffix:
echo [INFO] release\Infinite-Canvas-Setup-%BUILD_VERSION%.exe
echo.
echo [INFO] Building installer...
npm run build:win
if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo [OK] Done. Expected installer:
echo [OK] release\Infinite-Canvas-Setup-%BUILD_VERSION%.exe
pause
