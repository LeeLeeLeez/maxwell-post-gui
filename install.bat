@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=
title Maxwell Post GUI - Installer

where py >nul 2>nul && (py -3 install.py & goto :end)
where python >nul 2>nul && (python install.py & goto :end)
echo [ERROR] Python not found. Install Python 3.9+ from https://www.python.org/downloads/
pause

:end
