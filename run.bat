@echo off
setlocal

cd /d "%~dp0"
set "VENV_DIR=%cd%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%cd%\requirements.lock"
set "REQ_STAMP=%VENV_DIR%\.requirements-lock.sha256"

echo [DEBUG] Working directory: %cd%

set "BASE_PYTHON="
set "BASE_PYTHON_VERSION="
py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "BASE_PYTHON=py -3.11"
    set "BASE_PYTHON_VERSION=3.11"
)
if not defined BASE_PYTHON (
    py -3.10 --version >nul 2>&1
    if not errorlevel 1 (
        set "BASE_PYTHON=py -3.10"
        set "BASE_PYTHON_VERSION=3.10"
    )
)
if not defined BASE_PYTHON (
    echo [!] Python 3.10 or 3.11 is required. Install one and run this script again.
    pause
    exit /b 1
)

if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if f'{sys.version_info.major}.{sys.version_info.minor}' == '%BASE_PYTHON_VERSION%' else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [+] Existing virtual environment uses an incompatible Python version; recreating it...
        rmdir /s /q "%VENV_DIR%"
    )
)

if not exist "%VENV_PYTHON%" (
    echo [+] Creating local virtual environment...
    %BASE_PYTHON% -m venv "%VENV_DIR%"
    if %ERRORLEVEL% NEQ 0 goto error

    echo [+] Upgrading pip...
    "%VENV_PYTHON%" -m pip install --upgrade --no-cache-dir pip
    if %ERRORLEVEL% NEQ 0 goto error
)

call :sync_requirements
if %ERRORLEVEL% NEQ 0 goto error

if not exist "main.py" (
    echo [!] ERROR: File 'main.py' not found in current directory!
    goto error
)

echo [+] Starting main.py...
"%VENV_PYTHON%" "main.py" %*
goto end

:sync_requirements
if not exist "%REQ_FILE%" (
    echo [!] ERROR: Dependency lock file not found: "%REQ_FILE%"
    exit /b 1
)

set "CURRENT_HASH="
for /f "delims=" %%H in ('powershell -NoProfile -Command "(Get-FileHash -LiteralPath $env:REQ_FILE -Algorithm SHA256).Hash"') do set "CURRENT_HASH=%%H"
if not defined CURRENT_HASH (
    echo [!] ERROR: Could not calculate the dependency lock file hash.
    exit /b 1
)

set "SAVED_HASH="
if exist "%REQ_STAMP%" set /p SAVED_HASH=<"%REQ_STAMP%"
if /i "%CURRENT_HASH%"=="%SAVED_HASH%" exit /b 0

echo [+] Syncing project dependencies...
"%VENV_PYTHON%" -m pip install --quiet --disable-pip-version-check --require-hashes --no-cache-dir -r "%REQ_FILE%"
if %ERRORLEVEL% NEQ 0 exit /b 1

>"%REQ_STAMP%" echo %CURRENT_HASH%
exit /b 0

:error
echo.
echo [!] Script execution failed with error level %ERRORLEVEL%.
pause
exit /b %ERRORLEVEL%

:end
pause
