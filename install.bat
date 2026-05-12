@echo off
:: ═══════════════════════════════════════════════════════════════════════════════
:: Ares — Windows Installer
:: Supports: Windows 10, Windows 11 (both x64)
:: Detects NVIDIA GPU (RTX 3070 Ti, etc.) for CUDA acceleration
:: ═══════════════════════════════════════════════════════════════════════════════
setlocal EnableDelayedExpansion
title Ares — Installer

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║              Ares  Installer v5.1 (authoritative)                ║
echo  ╚══════════════════════════════════════════════════╝
echo.

:: ── Check Python 3.10+ ────────────────────────────────────────────────────────
echo [1/8] Checking Python...
set "PYTHON="
for %%p in (python3 python py) do (
    if "!PYTHON!"=="" (
        %%p -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if !errorlevel! == 0 (
            set "PYTHON=%%p"
            for /f "tokens=*" %%v in ('%%p --version 2^>^&1') do echo     Found: %%v
        )
    )
)

if "!PYTHON!"=="" (
    echo [!] Python 3.10+ not found.
    echo     Please install from: https://www.python.org/downloads/
    echo     Make sure to check "Add Python to PATH"
    pause
    exit /b 1
)
echo  [OK] Python found

:: ── Check Node.js ─────────────────────────────────────────────────────────────
echo [2/8] Checking Node.js...
node --version >nul 2>&1
if errorlevel 1 (
    echo [!] Node.js not found.
    echo     Please install Node.js 20+ from: https://nodejs.org
    echo     Attempting to open download page...
    start https://nodejs.org/en/download/
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node --version') do echo     Found: %%v
echo  [OK] Node.js found

:: ── Detect NVIDIA GPU (CUDA) ──────────────────────────────────────────────────
echo [3/8] Checking for NVIDIA GPU...
set "GPU_FOUND=false"
set "CUPY_PKG="
nvidia-smi >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%g in ('nvidia-smi --query-gpu^=name --format^=csv^,noheader 2^>nul') do (
        echo     GPU: %%g
        set "GPU_FOUND=true"
    )
    :: Get CUDA version
    for /f "tokens=5" %%c in ('nvidia-smi ^| findstr "CUDA Version"') do (
        echo     CUDA: %%c
        for /f "tokens=1 delims=." %%m in ("%%c") do set "CUDA_MAJOR=%%m"
    )
    if "!GPU_FOUND!"=="true" (
        set "CUPY_PKG=cupy-cuda!CUDA_MAJOR!x"
        echo  [OK] NVIDIA GPU detected — GPU acceleration enabled
    )
) else (
    echo  [!] No NVIDIA GPU detected — will use CPU mode
)

:: ── Create Python virtual environment ─────────────────────────────────────────
echo [4/8] Creating Python virtual environment...
set "VENV_DIR=%SCRIPT_DIR%\backend\.venv"
if not exist "%VENV_DIR%" (
    !PYTHON! -m venv "%VENV_DIR%"
)
call "%VENV_DIR%\Scripts\activate.bat"
python -m pip install --upgrade pip --quiet
echo  [OK] Virtual environment ready

:: ── Install CuPy if GPU found ─────────────────────────────────────────────────
if "!GPU_FOUND!"=="true" if not "!CUPY_PKG!"=="" (
    echo [4b] Installing CuPy for GPU acceleration...
    pip install !CUPY_PKG! --quiet && echo  [OK] CuPy installed || echo  [!] CuPy install failed - CPU mode only
)

:: ── Install Python dependencies ───────────────────────────────────────────────
echo [5/8] Installing Python dependencies...
pip install -r "%SCRIPT_DIR%\backend\requirements.txt" --quiet
echo  [OK] Python packages installed

:: ── Frontend ──────────────────────────────────────────────────────────────────
echo [6/8] Installing frontend dependencies...
cd /d "%SCRIPT_DIR%\frontend"
call npm install --silent
echo  [OK] Frontend packages installed

echo [7/8] Building frontend...
call npm run build --silent
echo  [OK] Frontend built

:: ── Electron ──────────────────────────────────────────────────────────────────
echo [8/8] Installing Electron desktop app...
cd /d "%SCRIPT_DIR%\electron"
call npm install --silent
echo  [OK] Electron installed

:: ── Create launcher scripts ───────────────────────────────────────────────────
echo Creating launcher scripts...

> "%SCRIPT_DIR%\start-backend.bat" (
    echo @echo off
    echo call "%VENV_DIR%\Scripts\activate.bat"
    echo cd /d "%SCRIPT_DIR%\backend"
    echo uvicorn app.main:app --host 0.0.0.0 --port 8000
)

> "%SCRIPT_DIR%\start-desktop.bat" (
    echo @echo off
    echo call "%VENV_DIR%\Scripts\activate.bat"
    echo cd /d "%SCRIPT_DIR%\electron"
    echo npx electron .
)

:: Create desktop shortcut using VBScript
set "SHORTCUT_PATH=%USERPROFILE%\Desktop\Ares.lnk"
> "%TEMP%\create_shortcut.vbs" (
    echo Set oWS = WScript.CreateObject^("WScript.Shell"^)
    echo sLinkFile = "%SHORTCUT_PATH%"
    echo Set oLink = oWS.CreateShortcut^(sLinkFile^)
    echo oLink.TargetPath = "%SCRIPT_DIR%\start-desktop.bat"
    echo oLink.WorkingDirectory = "%SCRIPT_DIR%"
    echo oLink.Description = "Ares"
    echo oLink.WindowStyle = 1
    echo oLink.Save
)
cscript //Nologo "%TEMP%\create_shortcut.vbs"
del "%TEMP%\create_shortcut.vbs"

:: Also create Start Menu entry
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Ares.lnk"
> "%TEMP%\create_shortcut2.vbs" (
    echo Set oWS = WScript.CreateObject^("WScript.Shell"^)
    echo sLinkFile = "%STARTMENU%"
    echo Set oLink = oWS.CreateShortcut^(sLinkFile^)
    echo oLink.TargetPath = "%SCRIPT_DIR%\start-desktop.bat"
    echo oLink.WorkingDirectory = "%SCRIPT_DIR%"
    echo oLink.Description = "Ares - RF propagation and geolocation platform"
    echo oLink.WindowStyle = 1
    echo oLink.Save
)
cscript //Nologo "%TEMP%\create_shortcut2.vbs"
del "%TEMP%\create_shortcut2.vbs"

echo.
echo  ╔══════════════════════════════════════════════════╗
echo  ║            Installation Complete! ✓              ║
echo  ╚══════════════════════════════════════════════════╝
echo.
echo   Desktop shortcut: Ares
echo   Start Menu: Ares
echo   Or run: start-desktop.bat
echo.
if "!GPU_FOUND!"=="true" (
    echo   GPU acceleration: ENABLED ^(CUDA^)
) else (
    echo   GPU acceleration: DISABLED ^(no NVIDIA GPU^)
)
echo.
pause
