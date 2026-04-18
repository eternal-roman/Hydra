@echo off
title HYDRA Trading Agent
cd /d "%~dp0"

echo ========================================
echo  HYDRA - Auto-Restart Launcher
echo ========================================
echo.

:: Start the CBP memory sidecar (idempotent; --detach is a no-op if
:: already running). Failure is intentionally swallowed — Hydra must
:: never block on the sidecar per cbp-runner/CLAUDE.md.
if not defined CBP_RUNNER_DIR set "CBP_RUNNER_DIR=C:\Users\elamj\Dev\cbp-runner"
if exist "%CBP_RUNNER_DIR%\supervisor.py" (
    echo [%date% %time%] Starting CBP sidecar ^(detached^) via %CBP_RUNNER_DIR%
    python "%CBP_RUNNER_DIR%\supervisor.py" --detach >nul 2>&1
)

:loop
echo [%date% %time%] Starting HYDRA agent...
python -u hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC --mode competition --resume
echo.
echo [%date% %time%] HYDRA exited (code %errorlevel%). Restarting in 10 seconds...
echo Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
