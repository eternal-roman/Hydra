@echo off
title HYDRA Dashboard
cd /d "%~dp0\dashboard"

echo ========================================
echo  HYDRA Dashboard Server
echo ========================================
echo.

:loop
echo [%date% %time%] Starting dashboard...
npm run dev
echo.
echo [%date% %time%] Dashboard exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
