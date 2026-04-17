@echo off
setlocal
cd /d "%~dp0\.."

set "TOOLS_DIR=%CD%\tools"
set "DL_DIR=%TOOLS_DIR%\downloads"
set "PY_DIR=%TOOLS_DIR%\python38-win32"
set "PY_EXE=%PY_DIR%\python.exe"
set "VENV_DIR=%CD%\.venv-win7-32"
set "INSTALLER=%DL_DIR%\python-3.8.10-win32.exe"

if exist "%PY_EXE%" goto :venv

if not exist "%DL_DIR%" mkdir "%DL_DIR%"

echo Downloading Python 3.8.10 x86...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.8.10/python-3.8.10.exe' -OutFile '%INSTALLER%'"
if errorlevel 1 goto :error

echo Installing Python 3.8.10 x86...
start /wait "" "%INSTALLER%" /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=0 Include_doc=0 Include_tcltk=1 Include_pip=1 TargetDir="%PY_DIR%"
if errorlevel 1 goto :error

:venv
echo Preparing Win7 x86 virtual environment...
"%PY_EXE%" -m venv "%VENV_DIR%"
if errorlevel 1 goto :error

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto :error

python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :error

python -m pip install -r requirements-win7.txt
if errorlevel 1 goto :error

echo Win7 x86 toolchain is ready.
goto :end

:error
echo Win7 x86 toolchain setup failed.
pause
exit /b 1

:end
endlocal
