@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment was not found.
    echo Run scripts\install.bat first.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate .venv
    pause
    exit /b 1
)

echo Starting DataFusion RT...
python app\main.py --config config\config.yaml
set EXIT_CODE=%ERRORLEVEL%

echo.
if not "%EXIT_CODE%"=="0" (
    echo Application exited with code %EXIT_CODE%.
) else (
    echo Application finished.
)
pause
exit /b %EXIT_CODE%
