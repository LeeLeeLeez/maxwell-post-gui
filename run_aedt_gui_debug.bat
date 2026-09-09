@echo off
cd /d "%~dp0"
set PYTHONPATH=
rem Keep the console open so tracebacks are visible. Use run_aedt_gui.bat for daily use.
where python >nul 2>nul && (python "%~dp0aedt_gui.py" & goto :end)
where py >nul 2>nul && (py -3 "%~dp0aedt_gui.py" & goto :end)
echo [ERROR] Python not found.
:end
pause
