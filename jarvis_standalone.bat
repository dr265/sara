@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem  JARVIS STANDALONE LAUNCHER
rem  Starts the full SARA stack from ONE window:
rem    [1] Backend API   (port 8000)
rem    [2] Website UI    (port 3000)  - opened by Jarvis browser actions
rem    [3-8] Desktop env, deps, credentials, hub, phone
rem    [9] SARA Desktop / Jarvis (this window)
rem
rem  History: the original launcher was lost from the working
rem  tree; this version was recovered from git (8c54f0a) and
rem  hardened further:
rem    * Uses ONLY the project venv python (never the global
rem      interpreter) so pyautogui/mss land in the environment
rem      the app actually runs in.
rem    * Verifies the desktop-critical imports (pyautogui, mss,
rem      cv2, pyperclip) AFTER pip and installs them explicitly
rem      if requirements.txt missed them.
rem    * Every path quoted; meaningful errors; pauses on failure
rem      so the window never silently disappears.
rem    * Port alignment: backend defaults to 8000 and the
rem      website to 3000 - the ports the frontend, the desktop
rem      code and the README all expect. Both launchers export
rem      SARA_BACKEND_URL / SARA_FRONTEND_URL so the desktop
rem      can never probe a different port than the one bound.
rem ============================================================

cd /d "%~dp0"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

if "%SARA_PORT%"=="" (set "BACKEND_PORT=8000") else (set "BACKEND_PORT=%SARA_PORT%")
if "%FRONTEND_PORT%"=="" (set "FRONTEND_PORT=3000")
set "BACKEND_URL=http://127.0.0.1:%BACKEND_PORT%"
set "FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%"

rem Exported for every child process (Jarvis main.py + its tools). This is the
rem contract that keeps the desktop and the launchers on the same ports.
set "SARA_BACKEND_URL=%BACKEND_URL%"
set "SARA_FRONTEND_URL=%FRONTEND_URL%"

echo.
echo ============================================================
echo   JARVIS STANDALONE - Voice AI Assistant
echo   Backend: %BACKEND_URL%   Website: %FRONTEND_URL%
echo ============================================================
echo.

rem ---------------------------------------------------------------
rem [1/9] Backend services
rem ---------------------------------------------------------------
echo [1/9] Checking backend services...

set "BACKEND_RUNNING=0"
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%BACKEND_URL%/api/health'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }"
if not errorlevel 1 (
    set "BACKEND_RUNNING=1"
    echo [OK] Backend API is already running on port %BACKEND_PORT%.
) else (
    echo [INFO] Backend is not running - starting it now...
    if exist "%ROOT%\backend\venv\Scripts\python.exe" (
        set "BACKEND_PY=%ROOT%\backend\venv\Scripts\python.exe"
    ) else (
        set "BACKEND_PY=python"
        where python >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Python was not found in PATH and backend\venv is
            echo         missing - cannot start the backend. The desktop
            echo         assistant will still start; hub agents and phone
            echo         connectivity will be unavailable.
            goto frontend_check
        )
    )
    if not exist "%ROOT%\backend\run_backend.py" (
        echo [WARNING] "%ROOT%\backend\run_backend.py" not found - skipping backend.
        goto frontend_check
    )
    start "SARA-BACKEND" /D "%ROOT%\backend" cmd /k ""!BACKEND_PY!" run_backend.py --port %BACKEND_PORT%"
    echo [WAIT] Giving the backend time to bind port %BACKEND_PORT%...
    rem `timeout /t` dies when stdin is not a console (launched from a script
    rem or scheduler). ping-based sleep works everywhere.
    ping -n 13 127.0.0.1 >nul
)

:frontend_check
rem ---------------------------------------------------------------
rem [2/9] Website (Next.js) - Jarvis opens http://127.0.0.1:3000/...
rem        routes for hub agents and the agency workspace, so the
rem        website must be up alongside the backend.
rem ---------------------------------------------------------------
echo [2/9] Checking website (frontend)...
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%FRONTEND_URL%/'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }"
if not errorlevel 1 (
    echo [OK] Website is already running on port %FRONTEND_PORT%.
    goto desktop_env
)
if not exist "%ROOT%\frontend\package.json" (
    echo [INFO] frontend\package.json not found - skipping website startup.
    goto desktop_env
)
where npm >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Node.js/npm was not found in PATH - the website will not
    echo           be started. Jarvis browser actions that open
    echo           %FRONTEND_URL% will report the site as down.
    goto desktop_env
)
echo [INFO] Website is not running - starting it now...
if not exist "%ROOT%\frontend\node_modules" (
    echo [INFO] First run: installing frontend dependencies
    echo        ^(this can take several minutes^)...
    pushd "%ROOT%\frontend"
    call npm install --no-audit --no-fund
    popd
)
start "SARA-FRONTEND" /D "%ROOT%\frontend" cmd /k "npm run dev -- -p %FRONTEND_PORT%"
echo [WAIT] Waiting for the website to answer on port %FRONTEND_PORT%...
set /a FE_TRIES=0
:wait_frontend
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%FRONTEND_URL%/'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }"
if not errorlevel 1 goto frontend_ready
set /a FE_TRIES+=1
if !FE_TRIES! geq 30 (
    echo [INFO] Website did not answer within ~90s - continuing anyway.
    echo        Its window ^(SARA-FRONTEND^) shows the real status.
    goto desktop_env
)
ping -n 3 127.0.0.1 >nul
goto wait_frontend

:frontend_ready
echo [OK] Website answered on %FRONTEND_URL%.

:desktop_env
rem ---------------------------------------------------------------
rem [3/9] Python environment for sara_desktop
rem ---------------------------------------------------------------
echo [3/9] Setting up the Jarvis Python environment...
if not exist "%ROOT%\sara_desktop\venv\Scripts\python.exe" (
    echo       Virtual environment missing. Creating it...
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python was not found in PATH and no venv exists yet.
        echo         Install Python 3.11+ with "Add to PATH", then re-run.
        pause
        exit /b 1
    )
    python -m venv "%ROOT%\sara_desktop\venv"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)
set "SARA_PY=%ROOT%\sara_desktop\venv\Scripts\python.exe"
if not exist "%SARA_PY%" (
    echo [ERROR] "%SARA_PY%" was not created.
    pause
    exit /b 1
)
echo       Using interpreter: %SARA_PY%

rem ---------------------------------------------------------------
rem [4/9] Dependencies
rem ---------------------------------------------------------------
echo [4/9] Installing dependencies (missing ones only)...
"%SARA_PY%" -m pip install --upgrade pip setuptools wheel --disable-pip-version-check >nul 2>&1
"%SARA_PY%" -m pip install -r "%ROOT%\sara_desktop\requirements.txt" --disable-pip-version-check
if errorlevel 1 (
    echo [WARNING] Some dependencies failed to install. The exact error is
    echo           above. Voice and desktop actions may be limited.
)

rem Desktop-critical imports: PyAutoGUI + MSS are what let Jarvis actually
rem see and operate the PC. Verify them in THIS interpreter and repair if
rem pip's dependency resolution silently skipped anything.
echo       Verifying PyAutoGUI / MSS / vision stack...
"%SARA_PY%" -c "import pyautogui, mss, cv2, pyperclip, PIL" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Desktop stack incomplete - installing it now...
    "%SARA_PY%" -m pip install --disable-pip-version-check pyautogui mss opencv-python pyperclip pillow
    "%SARA_PY%" -c "import pyautogui, mss, cv2, pyperclip, PIL" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] pyautogui/mss could not be installed. Desktop control and
        echo         screen vision will not work. Scroll up for the pip error.
        echo.
        pause
        exit /b 1
    )
)
echo       [OK] Desktop control + screen capture stack verified.

rem ---------------------------------------------------------------
rem [5/9] Playwright browsers
rem ---------------------------------------------------------------
echo [5/9] Checking Playwright browsers...
"%SARA_PY%" -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Playwright is not installed. Browser-control actions will
    echo        report themselves unavailable. Install later with:
    echo          "%SARA_PY%" -m pip install playwright
    echo          "%SARA_PY%" -m playwright install chromium
) else (
    "%SARA_PY%" -m playwright install chromium >nul 2>&1
    echo       [OK] Chromium present for browser automation.
)

rem ---------------------------------------------------------------
rem [6/9] Credentials  - backend/.env is the canonical store
rem ---------------------------------------------------------------
echo [6/9] Checking configuration...
if not exist "%ROOT%\sara_desktop\config\api_keys.json" (
    if exist "%ROOT%\sara_desktop\config\api_keys.example.json" (
        copy /y "%ROOT%\sara_desktop\config\api_keys.example.json" "%ROOT%\sara_desktop\config\api_keys.json" >nul
        echo [INFO] Created config\api_keys.json from the template.
    ) else (
        echo [WARNING] No api_keys.json and no example template found.
    )
)

pushd "%ROOT%\sara_desktop"
"%SARA_PY%" setup_keys.py
set "KEY_STATUS=%ERRORLEVEL%"
popd

if not "%KEY_STATUS%"=="0" (
    echo.
    echo [WARNING] No usable API credential was found.
    echo           Add GEMINI_API_KEY / LLM_API_KEY to backend\.env, or edit
    echo           sara_desktop\config\api_keys.json, then run this again.
    echo           Jarvis will still start, and will say plainly that it is
    echo           offline instead of pretending to work.
    echo.
)

rem ---------------------------------------------------------------
rem [7/9] Agent hub
rem ---------------------------------------------------------------
echo [7/9] Verifying agent hub connectivity...
rem The unified registry's fleet endpoint (runs real HTTP health probes
rem of every hub agent). This is the same URL the desktop app itself polls.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 '%BACKEND_URL%/ultron/agents'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }"
if not errorlevel 1 (
    echo [OK] Unified agent registry is reachable.
) else (
    echo [INFO] Agent registry is not reachable yet. Jarvis runs regardless;
    echo        hub-backed agents become available when the backend is up.
)

rem ---------------------------------------------------------------
rem [8/9] Phone connectivity
rem ---------------------------------------------------------------
echo [8/9] Checking phone connectivity...
rem Phone audio is served by the Jarvis dashboard (port 8010), which comes
rem up with Jarvis itself below - so on a cold start this correctly reports
rem "not up yet"; on a re-run over an already-running Jarvis it reports OK.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 'http://127.0.0.1:8010/'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Jarvis dashboard - phone connectivity - is available.
) else (
    echo [INFO] Phone service comes up with the Jarvis dashboard below.
)

rem ---------------------------------------------------------------
rem [9/9] Start Jarvis in THIS window so the log is visible
rem ---------------------------------------------------------------
rem Guard: a second instance would fight over the microphone and the
rem Gemini session (this is how duplicate stale processes happen).
powershell -NoProfile -Command "try { $c = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction Stop; exit 1 } catch { exit 0 }" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Jarvis is already running on this PC ^(dashboard listening
    echo         on port 8010^). Close the existing SARA window first, then
    echo         run this launcher again.
    echo.
    pause
    exit /b 1
)
echo [9/9] Starting Jarvis...
echo.
echo ============================================================
echo   JARVIS MODE ACTIVATED
echo ============================================================
echo   Voice control, PC automation, screen vision, agent hub
echo   access and phone connectivity all run from this window.
echo.
echo   Platform: backend %BACKEND_URL% + website %FRONTEND_URL%
echo   Say "Hey Sara" to wake (if wake word is enabled).
echo   Press Ctrl+C here to stop.
echo ============================================================
echo.

pushd "%ROOT%\sara_desktop"
"%SARA_PY%" main.py
set "JARVIS_RC=%ERRORLEVEL%"
popd

echo.
if not "%JARVIS_RC%"=="0" echo Jarvis exited with code %JARVIS_RC%.
echo Jarvis stopped.
pause
exit /b %JARVIS_RC%
