@echo off
setlocal

cd /d "%~dp0"
set "VENV_DIR=%cd%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=%cd%\requirements.txt"
set "REQ_STAMP=%VENV_DIR%\.requirements.stamp"

echo [DEBUG] Working directory: %cd%

if not exist "%VENV_PYTHON%" (
    echo [+] Creating local virtual environment...
    python -m venv "%VENV_DIR%"
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
if not exist "%REQ_FILE%" exit /b 0

xcopy /d /y "%REQ_FILE%" "%VENV_DIR%\" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    if exist "%REQ_STAMP%" exit /b 0
)

echo [+] Syncing project dependencies...
"%VENV_PYTHON%" -m pip install --upgrade --no-cache-dir -r "%REQ_FILE%"
if %ERRORLEVEL% NEQ 0 exit /b 1

type nul > "%REQ_STAMP%"
exit /b 0

:error
echo.
echo [!] Script execution failed with error level %ERRORLEVEL%.
pause
exit /b %ERRORLEVEL%

:end
pause