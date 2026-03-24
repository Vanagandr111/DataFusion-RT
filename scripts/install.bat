@echo off
setlocal
cd /d "%~dp0\.."

echo [1/4] Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv...
    py -3 -m venv .venv
    if errorlevel 1 goto :error
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 goto :error

echo [2/4] Upgrading pip...
python -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [3/4] Installing dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo [4/4] Installation completed.
echo Use scripts\start.bat to run the app.
goto :end

:error
echo Installation failed.
pause
exit /b 1

:end
endlocal
