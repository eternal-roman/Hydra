@echo off
title HYDRA Trading Agent (Companion Mode)
cd /d "%~dp0"

REM ═══════════════════════════════════════════════════════════════
REM  HYDRA launcher with the Companion subsystem enabled.
REM
REM  Flags set here:
REM    HYDRA_COMPANION_ENABLED=1            chat drawer active
REM    HYDRA_COMPANION_PROPOSALS_ENABLED=1  trade cards render
REM
REM  Intentionally NOT set (flip manually once you've tested):
REM    HYDRA_COMPANION_LIVE_EXECUTION=1     real orders (paper mode
REM                                         is on by default here)
REM    HYDRA_COMPANION_NUDGES=1             proactive messages
REM
REM  This launcher runs in --paper mode so no real orders land even
REM  if you set HYDRA_COMPANION_LIVE_EXECUTION=1 accidentally. Copy
REM  start_hydra.bat for the production incantation.
REM ═══════════════════════════════════════════════════════════════

set HYDRA_COMPANION_ENABLED=1
set HYDRA_COMPANION_PROPOSALS_ENABLED=1

echo ========================================
echo  HYDRA - Companion Mode (Paper)
echo ========================================
echo   Companion subsystem: ENABLED
echo   Proposals:           ENABLED (mock exec)
echo   Live execution:      OFF
echo   Proactive nudges:    OFF
echo   Trade mode:          --paper
echo ========================================
echo.

:loop
echo [%date% %time%] Starting HYDRA agent...
python -u hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC --mode competition --paper
echo.
echo [%date% %time%] HYDRA exited (code %errorlevel%). Restarting in 10 seconds...
echo Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
