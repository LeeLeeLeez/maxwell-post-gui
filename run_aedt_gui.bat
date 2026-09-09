@echo off
cd /d "%~dp0"
set PYTHONPATH=
rem Try pythonw first (no console window), then the py launcher.
where pythonw >nul 2>nul && (start "" pythonw "%~dp0aedt_gui.py" & exit /b 0)
where py >nul 2>nul && (start "" py -3 "%~dp0aedt_gui.py" & exit /b 0)
echo [ERROR] Python not found. Install Python 3.9+ and tick "Add to PATH".
pause
