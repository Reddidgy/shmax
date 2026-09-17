@echo off
REM Praxis Local API launcher for Windows.
REM Finds Python 3.10+, creates a venv, installs deps, and starts the server.
REM Usage: .praxis\api\start.bat
REM Debug: start.bat --debug  or  set PRAXIS_DEBUG=1

setlocal enabledelayedexpansion

REM --- Debug mode ---------------------------------------------------------------
set "DEBUG=0"
if "%~1"=="--debug" set "DEBUG=1"
if defined PRAXIS_DEBUG set "DEBUG=1"

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
if "!DEBUG!"=="1" echo [DEBUG] SCRIPT_DIR=!SCRIPT_DIR!

set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "REQ_FILE=%SCRIPT_DIR%\requirements.txt"
set "API_SCRIPT=%SCRIPT_DIR%\praxis_wrapper.py"
set "MIN_PYTHON_MINOR=10"

if "!DEBUG!"=="1" (
    echo [DEBUG] VENV_DIR=!VENV_DIR!
    echo [DEBUG] REQ_FILE=!REQ_FILE!
    echo [DEBUG] API_SCRIPT=!API_SCRIPT!
)

REM --- Find a Python >= 3.10 ---------------------------------------------------
set "PYTHON="
for %%P in (python3 python) do (
    if not defined PYTHON (
        where %%P >nul 2>&1
        if !errorlevel! equ 0 (
            for /f "tokens=*" %%V in ('%%P -c "import sys; print(sys.version_info.minor)" 2^>nul') do (
                if %%V geq %MIN_PYTHON_MINOR% (
                    for /f "tokens=*" %%A in ('where %%P 2^>nul') do (
                        if not defined PYTHON set "PYTHON=%%A"
                    )
                )
            )
        )
    )
)

if not defined PYTHON (
    echo ERROR: Python 3.10 or newer is required but not found.
    echo.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    echo Then run this script again.
    exit /b 1
)

for /f "tokens=*" %%V in ('"%PYTHON%" --version 2^>^&1') do set "PYTHON_VER=%%V"
echo Using %PYTHON_VER% (%PYTHON%)

REM --- Create venv if needed ----------------------------------------------------
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    "%PYTHON%" -m venv "%VENV_DIR%"
)

set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"
if "!DEBUG!"=="1" echo [DEBUG] VENV_PYTHON=!VENV_PYTHON!

REM --- Install / update deps ---------------------------------------------------
REM Re-install when .deps_installed is missing OR requirements.txt is newer
REM (matches start.sh's -nt check so new dependencies are picked up).
set "NEED_INSTALL=0"
if not exist "%VENV_DIR%\.deps_installed" (
    set "NEED_INSTALL=1"
) else (
    "%VENV_PYTHON%" -c "import os,sys; sys.exit(0 if os.path.getmtime(r'%REQ_FILE%') > os.path.getmtime(r'%VENV_DIR%\.deps_installed') else 1)" 2>nul && set "NEED_INSTALL=1"
)
if "!NEED_INSTALL!"=="1" (
    echo Installing dependencies...
    "%VENV_PIP%" install -q -r "%REQ_FILE%" && type nul > "%VENV_DIR%\.deps_installed"
)

REM --- Ensure .mcp.json has the praxis MCP entry --------------------------------
for %%I in ("%SCRIPT_DIR%\..") do set "PARENT_DIR=%%~fI"
for %%I in ("%PARENT_DIR%") do set "PARENT_NAME=%%~nxI"

if "%PARENT_NAME%"==".praxis" (
    for %%I in ("%PARENT_DIR%\..") do set "PROJECT_ROOT=%%~fI"
) else (
    set "PROJECT_ROOT=%PARENT_DIR%"
)

set "MCP_JSON=%PROJECT_ROOT%\.mcp.json"
if defined LOCAL_API_PORT (set "MCP_PORT=%LOCAL_API_PORT%") else (
    if defined CHAT_API_PORT (set "MCP_PORT=%CHAT_API_PORT%") else (set "MCP_PORT=7865")
)

if "!DEBUG!"=="1" (
    echo [DEBUG] PARENT_DIR=!PARENT_DIR!
    echo [DEBUG] PARENT_NAME=!PARENT_NAME!
    echo [DEBUG] PROJECT_ROOT=!PROJECT_ROOT!
    echo [DEBUG] MCP_JSON=!MCP_JSON!
    echo [DEBUG] MCP_PORT=!MCP_PORT!
)

if "!DEBUG!"=="1" echo [DEBUG] Running provision_mcp_json.py...
"%VENV_PYTHON%" "%SCRIPT_DIR%\provision_mcp_json.py" "%MCP_JSON%" "%MCP_PORT%"
if "!DEBUG!"=="1" echo [DEBUG] provision_mcp_json.py exit code: !errorlevel!

REM --- Kill stale instance if pidfile exists ------------------------------------
set "OLD_PID="
set "PID_FILE=%SCRIPT_DIR%\praxis.pid"
if "!DEBUG!"=="1" echo [DEBUG] PID_FILE=!PID_FILE!

if exist "!PID_FILE!" (
    set /p OLD_PID=<"!PID_FILE!"
    del /q "!PID_FILE!" 2>nul
)

if defined OLD_PID (
    if "!DEBUG!"=="1" echo [DEBUG] Found stale PID: !OLD_PID! - killing...
    echo Stopping previous instance, PID !OLD_PID!
    taskkill /F /T /PID !OLD_PID! >nul 2>&1
)

REM --- Rotate an oversized log before launch (POS-1995) -------------------------
REM Mirrors start.sh: the RotatingFileHandler caps praxis.log at 5 MB, but an
REM unclean shutdown can leave an oversized file behind. Roll it here so a fresh
REM session always starts inside the size budget.
set "LOG_FILE=%SCRIPT_DIR%\praxis.log"
set "LOG_MAX_BYTES=5242880"
set "LOG_SIZE=0"
if exist "!LOG_FILE!" (
    for %%I in ("!LOG_FILE!") do set "LOG_SIZE=%%~zI"
)
if "!DEBUG!"=="1" echo [DEBUG] LOG_FILE=!LOG_FILE! LOG_SIZE=!LOG_SIZE!
if !LOG_SIZE! GEQ !LOG_MAX_BYTES! (
    echo Rotating oversized log, !LOG_SIZE! bytes, to praxis.log.1
    move /Y "!LOG_FILE!" "!LOG_FILE!.1" >nul 2>&1
)

REM --- Start the API in the background -----------------------------------------
REM Append (>>), never truncate (>): the Python RotatingFileHandler owns this
REM file now. This redirect only catches raw-fd output from a bootstrap failure
REM before Python logging is configured.
echo.
if "!DEBUG!"=="1" echo [DEBUG] Starting: "!VENV_PYTHON!" "!API_SCRIPT!"
start /B "" "%VENV_PYTHON%" "%API_SCRIPT%" >> "%LOG_FILE%" 2>&1

REM Small delay to let the process start and write its PID file
timeout /t 2 /nobreak >nul

if exist "!PID_FILE!" (
    set /p WRAPPER_PID=<"!PID_FILE!"
    echo Praxis Local API started in background, PID !WRAPPER_PID!
    echo Log file: !LOG_FILE! ^(rotates at 5 MB, 2 backups kept^)
    echo To stop: taskkill /F /T /PID !WRAPPER_PID!
) else (
    echo Praxis Local API started in background.
    echo Log file: !LOG_FILE!
    if "!DEBUG!"=="1" echo [DEBUG] PID file not found after startup - server may have failed to start
    if "!DEBUG!"=="1" echo [DEBUG] Check log file for errors: !LOG_FILE!
)
