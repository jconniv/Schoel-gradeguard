@echo off
cd /d "%~dp0"
python build_exe.py
if errorlevel 1 exit /b 1
pause
