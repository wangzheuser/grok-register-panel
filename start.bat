@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo [Startup] Checking runtime...

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :check_packages

set "BASE_PY="
py -3 --version >nul 2>&1 && set "BASE_PY=py -3"
if defined BASE_PY goto :create_venv
python --version >nul 2>&1 && set "BASE_PY=python"
if not defined BASE_PY (
    echo [Error] Python 3 was not found. Install Python and add it to PATH.
    goto :failed
)

:create_venv
echo [Dependencies] Creating virtual environment .venv...
%BASE_PY% -m venv ".venv"
if errorlevel 1 goto :failed

:check_packages
"%VENV_PY%" -c "import DrissionPage, curl_cffi, camoufox, playwright, requests, psutil" >nul 2>&1
if errorlevel 1 (
    echo [Dependencies] Installing Python packages...
    "%VENV_PY%" -m pip install -r "requirements.txt"
    if errorlevel 1 goto :failed
) else (
    echo [Dependencies] Python packages are ready.
)

"%VENV_PY%" -c "from camoufox.pkgman import camoufox_path; import sys; sys.exit(0 if camoufox_path().exists() else 1)" >nul 2>&1
if errorlevel 1 (
    echo [Dependencies] Installing the Camoufox browser...
    "%VENV_PY%" -m camoufox fetch
    if errorlevel 1 goto :failed
) else (
    echo [Dependencies] The Camoufox browser is ready.
)

if not exist "config.json" (
    if not exist "config.example.json" (
        echo [Error] config.json and config.example.json were not found.
        goto :failed
    )
    copy /y "config.example.json" "config.json" >nul
    echo [Config] Created config.json from config.example.json.
)

if not defined MONITOR_TOKEN if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /I "%%A"=="MONITOR_TOKEN" set "MONITOR_TOKEN=%%B"
    )
)
if defined MONITOR_TOKEN echo [Config] Loaded MONITOR_TOKEN from environment or .env.

if not defined MONITOR_TOKEN (
    echo [Config] MONITOR_TOKEN is not set.
    set /p "MONITOR_TOKEN=Enter panel password (visible): "
)
if not defined MONITOR_TOKEN (
    echo [Error] The panel password cannot be empty.
    goto :failed
)

echo [Startup] Panel URL: http://127.0.0.1:8787
"%VENV_PY%" "webui\monitor.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [Error] Service exited with code %EXIT_CODE%.
    exit /b %EXIT_CODE%
)
exit /b 0

:failed
echo [Error] Startup preparation failed.
exit /b 1
