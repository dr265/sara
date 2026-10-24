@echo off
REM ---------------------------------------------------------------------------
REM SARA voice-agent tunnel watchdog.
REM
REM WHY: Twilio can only reach SARA through this tunnel. When ngrok dies, every
REM inbound call gets no response and the caller hears Twilio's "an application
REM error has occurred" — which looks like a voice-agent bug but is a dead
REM tunnel. ngrok has been observed dying silently several times, so we keep a
REM cheap health check running and restart it automatically.
REM
REM Usage:  run_tunnel_watchdog.bat
REM ---------------------------------------------------------------------------
REM `!FAILS!` requires delayed expansion, so it is enabled here.
setlocal enabledelayedexpansion
set NGROK=C:\Users\HP\OneDrive\Desktop\n8n\ngrok.exe
set DOMAIN=ungrotesque-eneida-ripe.ngrok-free.dev
set HEALTH=https://%DOMAIN%/api/health
set LOG=%~dp0tunnel_watchdog.log

echo [%DATE% %TIME%] watchdog started >> "%LOG%"

REM Only ONE watchdog may run. Without this guard, starting the backend more
REM than once (or double-clicking the file) leaves several loops racing to
REM kill and restart ngrok, which is how the tunnel ended up dead while logs
REM claimed a watchdog was "running".
for /f "tokens=2 delims=," %%p in ('tasklist /fi "imagename eq cmd.exe" /fo csv /nh 2^>nul ^| findstr /i "run_tunnel_watchdog"') do (
  echo [%DATE% %TIME%] another watchdog already running - exiting >> "%LOG%"
  exit /b 0
)

:loop
REM 1. Is the public URL actually answering SARA? (This is the real check —
REM    a running ngrok process can still have a dead tunnel.)
REM    Two consecutive failures are required before restarting, so a brief
REM    outage while the backend itself is restarting never kills the tunnel
REM    and turns one restart into a restart loop.
set /a FAILS=0
:check
powershell -NoProfile -Command ^
  "try{$r=Invoke-WebRequest -Uri '%HEALTH%' -Headers @{'ngrok-skip-browser-warning'='true'} -UseBasicParsing -TimeoutSec 20; if($r.StatusCode -eq 200){exit 0}else{exit 1}}catch{exit 1}" >nul 2>&1
if errorlevel 1 (
  set /a FAILS+=1
  if !FAILS! GEQ 2 goto :restart
  timeout /t 15 /nobreak >nul
  goto :check
)

REM 2. Healthy. Reset the failure count and re-check later.
set /a FAILS=0
timeout /t 45 /nobreak >nul
goto :loop

:restart
echo [%DATE% %TIME%] tunnel DOWN - restarting >> "%LOG%"
set /a FAILS=0
taskkill /IM ngrok.exe /F >nul 2>&1
timeout /t 3 /nobreak >nul
REM ngrok resolves "localhost" to IPv6 [::1] first, but the server binds IPv4
REM 0.0.0.0 only — so the tunnel dies with "No connection could be made
REM because the target machine actively refused it" every time the backend
REM restarts. Forwarding to 127.0.0.1:8000 forces IPv4 and removes that class
REM of failure entirely.
start "" "%NGROK%" http 127.0.0.1:8000 --domain=%DOMAIN% --log stdout --log-format json
REM Give the new session time to establish before we judge it again.
timeout /t 25 /nobreak >nul
echo [%DATE% %TIME%] tunnel restarted >> "%LOG%"
goto :loop
