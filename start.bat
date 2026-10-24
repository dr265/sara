@echo off
setlocal EnableDelayedExpansion

cd /d "%~dp0"

rem ============================================================
rem  SARA AI PLATFORM - Election Campaign Voice Agent launcher
rem
rem  Starts exactly what the Dynamic Election Campaign Voice Agent
rem  needs, in the right order:
rem    1. Backend API (port 8000) - includes the voice engine, the
rem       Twilio provider and the /voice-agent/voice/campaign/* API
rem    2. Frontend Web UI (port 3000) - Next.js dev server
rem  and opens the Election Campaign console in the browser.
rem
rem  SARA Desktop / JARVIS is deliberately NOT started here.
rem
rem  Usage:
rem    start.bat          # Start backend + frontend, open the console
rem    start.bat stop     # Stop both servers (those two ports only)
rem    start.bat status   # Show what is running
rem ============================================================

if /i "%~1"=="stop" goto stop
if /i "%~1"=="status" goto status

if "%SARA_PORT%"=="" (set BACKEND_PORT=8000) else (set BACKEND_PORT=%SARA_PORT%)
if "%FRONTEND_PORT%"=="" (set FRONTEND_PORT=3000) else (set FRONTEND_PORT=%FRONTEND_PORT%)

set "SARA_BACKEND_URL=http://127.0.0.1:%BACKEND_PORT%"
set "SARA_FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%"
set "CAMPAIGN_URL=%SARA_FRONTEND_URL%/hub/voice-agent#campaign"

echo.
echo ============================================================
echo   SARA AI - Election Campaign Voice Agent
echo ============================================================
echo   Backend API   : %SARA_BACKEND_URL%
echo   Frontend Web  : %SARA_FRONTEND_URL%
echo   Console       : %CAMPAIGN_URL%
echo.
echo   Configure the agent at  Voice Agent - Election Campaign
echo   (candidate, organisation, greeting, FAQs, hand-off, schedule).
echo.

rem ---------------------------------------------------------------
rem Preflight
rem ---------------------------------------------------------------
if not exist "backend\.env" (
    echo [WARN] backend\.env not found. Copy .env.example to backend\.env
    echo        and fill in TWILIO_*, SARVAM_API_KEY and LLM_API_KEY,
    echo        otherwise calls and speech will not work.
    echo.
)

if not exist "frontend\node_modules" (
    echo [ERROR] frontend\node_modules is missing.
    echo         Run: npm --prefix frontend install
    pause
    exit /b 1
)

set PYTHON_CMD=
if exist "%~dp0backend\venv\Scripts\python.exe" goto python_venv
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found. Install Python 3.11+ with "Add to PATH",
    echo         or create backend\venv.
    pause
    exit /b 1
)
set PYTHON_CMD=python
goto python_ready
:python_venv
set PYTHON_CMD=%~dp0backend\venv\Scripts\python.exe
:python_ready

rem ---------------------------------------------------------------
rem Free the ports (only the two this script owns)
rem ---------------------------------------------------------------
echo Checking port availability...
for %%p in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort %%p -State Listen -ErrorAction SilentlyContinue; if ($conns) { $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('  Releasing port %%p from PID ' + $_); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } else { Write-Host ('  Port %%p is free') }"
)

rem ---------------------------------------------------------------
rem [1/2] Backend API
rem ---------------------------------------------------------------
echo [1/2] Starting Backend API on port %BACKEND_PORT%...
start "SARA-BACKEND" cmd /k "cd /d "%~dp0backend" && "%PYTHON_CMD%" run_backend.py --port %BACKEND_PORT%"

set /a TRIES=0
:wait_backend
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%SARA_BACKEND_URL%/api/health'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto backend_ready
set /a TRIES+=1
if %TRIES% geq 45 (
    echo [ERROR] Backend did not become healthy after ~90s. Check the
    echo         SARA-BACKEND window for the startup error.
    pause
    exit /b 1
)
timeout /t 2 /nobreak >nul
goto wait_backend
:backend_ready
echo [OK] Backend API is healthy.

rem The campaign API must be mounted - a 401 (no credentials) still proves
rem the route exists, while a 404 would mean the voice engine is missing.
set /a TRIES=0
:wait_campaign
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%SARA_BACKEND_URL%/voice-agent/voice/status'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto campaign_ready
set /a TRIES+=1
if %TRIES% geq 10 goto campaign_ready
timeout /t 2 /nobreak >nul
goto wait_campaign
:campaign_ready
echo [OK] Voice engine is up (campaign API at /voice-agent/voice/campaign).

rem ---------------------------------------------------------------
rem [2/2] Frontend Web UI
rem ---------------------------------------------------------------
echo [2/2] Starting Frontend Web UI on port %FRONTEND_PORT%...
start "SARA-FRONTEND" cmd /k "cd /d "%~dp0frontend" && call npm run dev -- -p %FRONTEND_PORT%"

set /a TRIES=0
:wait_frontend
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %FRONTEND_PORT% -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto frontend_ready
set /a TRIES+=1
if %TRIES% geq 45 (
    echo [ERROR] Frontend did not start on port %FRONTEND_PORT%. Check the
    echo         SARA-FRONTEND window for the error.
    pause
    exit /b 1
)
timeout /t 2 /nobreak >nul
goto wait_frontend
:frontend_ready
echo [OK] Frontend is serving on port %FRONTEND_PORT%.

echo.
echo ============================================================
echo   READY
echo ============================================================
echo   Console : %CAMPAIGN_URL%
echo   Backend : %SARA_BACKEND_URL%
echo.
echo   Servers run in the SARA-BACKEND and SARA-FRONTEND windows.
echo   Close a window to stop that server, or run: start.bat stop
echo.

start "" "%CAMPAIGN_URL%"
exit /b 0


:stop
if "%SARA_PORT%"=="" (set BACKEND_PORT=8000) else (set BACKEND_PORT=%SARA_PORT%)
if "%FRONTEND_PORT%"=="" (set FRONTEND_PORT=3000) else (set FRONTEND_PORT=%FRONTEND_PORT%)

echo Stopping SARA servers on ports %BACKEND_PORT% and %FRONTEND_PORT% only...
for %%p in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort %%p -State Listen -ErrorAction SilentlyContinue; if ($conns) { $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Write-Host ('  Killing PID ' + $_ + ' on port %%p'); Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } } else { Write-Host ('  Nothing listening on port %%p') }"
)
echo Done.
pause
exit /b 0


:status
if "%SARA_PORT%"=="" (set BACKEND_PORT=8000) else (set BACKEND_PORT=%SARA_PORT%)
if "%FRONTEND_PORT%"=="" (set FRONTEND_PORT=3000) else (set FRONTEND_PORT=%FRONTEND_PORT%)

echo SARA AI - Election Campaign Voice Agent status
for %%p in (%BACKEND_PORT% %FRONTEND_PORT%) do (
    powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort %%p -State Listen -ErrorAction SilentlyContinue; if ($conns) { Write-Host ('  port %%p : RUNNING (PID ' + ($conns | Select-Object -First 1 -ExpandProperty OwningProcess) + ')') } else { Write-Host ('  port %%p : stopped') }"
)
echo.
echo Console: http://127.0.0.1:%FRONTEND_PORT%/hub/voice-agent#campaign
pause
exit /b 0
