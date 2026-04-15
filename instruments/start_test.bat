@echo off
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%..\"
set "LOG_DIR=%PROJECT_ROOT%logs"

chcp 65001 > nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ==========================================
echo DataFusion RT - RS485 Lab Tool
echo ==========================================
echo.
echo Скрипт: %SCRIPT_DIR%rs485_lab_tool.py
echo Проект: %PROJECT_ROOT%
echo.

if exist "%PROJECT_ROOT%.venv\Scripts\python.exe" (
    set "PYEXE=%PROJECT_ROOT%.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

echo Исполняемый файл Python: %PYEXE%
if not exist "%SCRIPT_DIR%rs485_lab_tool.py" (
    echo ОШИБКА: Скрипт не найден!
    pause
    exit /b 1
)

echo.
echo Запуск инструмента...
echo.

"%PYEXE%" "%SCRIPT_DIR%rs485_lab_tool.py" %*

echo.
echo Инструмент завершил работу.
pause
