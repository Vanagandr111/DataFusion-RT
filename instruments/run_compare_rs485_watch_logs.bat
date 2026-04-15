@echo off
setlocal

cd /d "%~dp0.."

chcp 65001 > nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ==========================================
echo DataFusion RT - RS485 Watch Log Compare
echo ==========================================
echo.

if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

echo Script: instruments\compare_rs485_watch_logs.py
echo Project: %CD%
echo Python: %PYEXE%
echo.

"%PYEXE%" instruments\compare_rs485_watch_logs.py %*

echo.
echo Compare finished.
pause
