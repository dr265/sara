@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

rem ============================================================
rem  SARA AI PLATFORM - Complete Launcher
rem
rem  Starts all components in the correct order:
rem    1. Backend API (port 8000) - FastAPI server, engine, agents
rem    2. Frontend Web UI (port 3000) - Next.js dev server
rem    3. SARA Desktop / JARVIS (this window) - Voice AI, PC automation
rem
rem  Usage:
rem    start_all.bat          # Start everything
rem    start_all.bat stop     # Stop all servers
rem ============================================================

if /i "%~1"=="stop" goto stop

if "%SARA_PORT%"=="" (set BACKEND_PORT=8000) else (set BACKEND_PORT=%SARA_PORT%)
if "%FRONTEND_PORT%"=="" (set FRONTEND_PORT=3000) else (set FRONTEND_PORT=%FRONTEND_PORT%)

rem Keep the desktop and its tools on the same ports this script binds.
set "SARA_BACKEND_URL=http://127.0.0.1:%BACKEND_PORT%"
set "SARA_FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%"

echo.
echo === SARA AI PLATFORM - Complete Launch ===
echo   Backend API   : http://127.0.0.1:%BACKEND_PORT%
echo   Frontend Web  : http://127.0.0.1:%FRONTEND_PORT%
echo   SARA Desktop  : Voice AI + PC Automation (this window)
echo.

echo Checking port availability...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /r ":%BACKEND_PORT% .*LISTENING"') do (
    echo Releasing port %BACKEND_PORT% from PID %%a...
    taskkill /f /pid %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /r ":%FRONTEND_PORT% .*LISTENING"') do (
    echo Releasing port %FRONTEND_PORT% from PID %%a...
    taskkill /f /pid %%a >nul 2>&1
)

echo Ports clear. Initializing servers...

rem Prefer project venv for backend, then fall back to PATH python.
set PYTHON_CMD=python
if exist "backend\venv\Scripts\python.exe" set PYTHON_CMD=venv\Scripts\python.exe

rem ---------------------------------------------------------------
rem [1/5] Launch Backend API
rem ---------------------------------------------------------------
echo [1/5] Starting Backend API on port %BACKEND_PORT%...
start "SARA-BACKEND" cmd /k "cd /d %~dp0backend && %PYTHON_CMD% run_backend.py --port %BACKEND_PORT%"

rem Wait for backend health endpoint before continuing
set /a TRIES=0
:wait_backend
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:%BACKEND_PORT%/api/health'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }"
if not errorlevel 1 goto backend_ready
set /a TRIES+=1
if %TRIES% geq 30 (
    echo WARNING: Backend not healthy after ~60s, continuing anyway...
    goto backend_ready
)
timeout /t 2 /nobreak >nul
goto wait_backend
:backend_ready
echo [OK] Backend API is running.

rem ---------------------------------------------------------------
rem [2/5] Launch Frontend Web UI
rem ---------------------------------------------------------------
echo [2/5] Starting Frontend Web UI on port %FRONTEND_PORT%...
start "SARA-FRONTEND" cmd /k "cd /d %~dp0frontend && call npm run dev -- -p %FRONTEND_PORT%"

echo.
echo Servers launched in separate windows:
echo   SARA-BACKEND   : backend logs (uvicorn + engine + agents)
echo   SARA-FRONTEND  : frontend logs (Next.js dev server)
echo.
echo Close either window to stop that server, or run: start_all.bat stop
echo.

rem ---------------------------------------------------------------
rem [3/5] Wait for backend to be fully ready (engine, agents)
rem ---------------------------------------------------------------
echo [3/5] Waiting for backend services (engine, agents)...
set /a TRIES=0
:wait_services
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:%BACKEND_PORT%/ultron/agents'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto services_ready
set /a TRIES+=1
if %TRIES% geq 15 (
    echo [INFO] Agent registry not ready yet; SARA Desktop will start anyway.
    goto services_ready
)
timeout /t 2 /nobreak >nul
goto wait_services
:services_ready
echo [OK] Backend services are available.

rem ---------------------------------------------------------------
rem [4/5] Setup SARA Desktop Python environment
rem ---------------------------------------------------------------
echo [4/5] Setting up SARA Desktop environment...
if not exist "sara_desktop\venv\Scripts\python.exe" (
    echo       Virtual environment missing. Creating it...
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python was not found in PATH and no venv exists yet.
        echo         Install Python 3.11+ with "Add to PATH", then re-run.
        pause
        exit /b 1
    )
    python -m venv "sara_desktop\venv"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)
set "SARA_PY=%~dp0sara_desktop\venv\Scripts\python.exe"
if not exist "%SARA_PY%" (
    echo [ERROR] "%SARA_PY%" was not created.
    pause
    exit /b 1
)
echo       Using interpreter: %SARA_PY%

echo [5/5] Installing dependencies...
"%SARA_PY%" -m pip install --upgrade pip setuptools wheel --disable-pip-version-check >nul 2>&1
"%SARA_PY%" -m pip install -r "sara_desktop\requirements.txt" --disable-pip-version-check
if errorlevel 1 (
    echo [WARNING] Some dependencies failed to install. Voice/desktop actions may be limited.
)

rem Verify desktop-critical imports (pyautogui, mss, cv2, pyperclip, PIL)
echo       Verifying desktop control stack...
"%SARA_PY%" -c "import pyautogui, mss, cv2, pyperclip, PIL" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Desktop stack incomplete - installing it now...
    "%SARA_PY%" -m pip install --disable-pip-version-check pyautogui mss opencv-python pyperclip pillow
    "%SARA_PY%" -c "import pyautogui, mss, cv2, pyperclip, PIL" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] pyautogui/mss could not be installed. Desktop control will not work.
        pause
        exit /b 1
    )
)
echo       [OK] Desktop control + screen capture stack verified.

rem Verify Playwright / Chromium
"%SARA_PY%" -c "import playwright" >nul 2>&1
if not errorlevel 1 (
    "%SARA_PY%" -m playwright install chromium >nul 2>&1
    echo       [OK] Chromium present for browser automation.
)

rem ---------------------------------------------------------------
rem [6/6] Start SARA Desktop / JARVIS in THIS window
rem ---------------------------------------------------------------
echo.
echo ============================================================
echo   SARA DESKTOP / JARVIS STARTING
echo ============================================================
echo   Voice control, PC automation, screen vision, agent hub
echo   access and phone connectivity all run from this window.
echo.
echo   Say "Hey Sara" to wake (if wake word is enabled).
echo   Press Ctrl+C here to stop.
echo ============================================================
echo.

pushd "%~dp0sara_desktop"
"%SARA_PY%" main.py
set "JARVIS_RC=%ERRORLEVEL%"
popd

echo.
if not "%JARVIS_RC%"=="0" echo SARA Desktop exited with code %JARVIS_RC%.
echo SARA Desktop stopped.
pause
exit /b %JARVIS_RC%

:stop
if "%SARA_PORT%"=="" (set BACKEND_PORT=8000) else (set BACKEND_PORT=%SARA_PORT%)
if "%FRONTEND_PORT%"=="" (set FRONTEND_PORT=3000) else (set FRONTEND_PORT=%FRONTEND_PORT%)

echo Closing SARA servers on ports %BACKEND_PORT% and %FRONTEND_PORT% only...
for %%p in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort %%p -State Listen -ErrorAction SilentlyContinue; if ($conns) { $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('Killing PID ' + $_ + ' on port %%p'); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } else { Write-Host ('Nothing listening on port %%p') }"
)

echo Also stopping SARA Desktop (port 8010)...
powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue; if ($conns) { $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('Killing PID ' + $_ + ' on port 8010'); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } else { Write-Host ('Nothing listening on port 8010') }"

echo Done.
pause
exit /b 0