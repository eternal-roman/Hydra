@echo off
echo ========================================
echo  HYDRA - Desktop Installer Builder
echo ========================================
echo.
echo Installing PyInstaller...
pip install pyinstaller

echo.
echo Building Hydra Agent Executable...
:: Build a one-file executable that doesn't open a console window (if GUI) 
:: For Hydra, we want the console so we omit --noconsole for now.
pyinstaller --name HydraAgent --onefile hydra_agent.py

echo.
echo Building Dashboard Executable (Electron wrapper placeholder)...
echo (Assuming Node.js is installed. In a full desktop pipeline, you would 
echo use Electron Forge or Tauri here to wrap dashboard/dist).

echo.
echo Build Complete! Check the 'dist' folder for the Executable.
pause
