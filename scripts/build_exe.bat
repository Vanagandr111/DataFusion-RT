@echo off
setlocal
cd /d "%~dp0\.."

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=modern"

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

for /f %%i in ('python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"') do set "PYVER=%%i"
for /f %%i in ('python -c "import struct; print(struct.calcsize('P') * 8)"') do set "PYBITS=%%i"

if /I "%TARGET%"=="modern" (
    echo Building modern Windows package with Python %PYVER% %PYBITS%-bit...
    echo Target: Windows 10/11 and similar modern systems.
)

if /I "%TARGET%"=="win7" (
    echo Preparing legacy Windows 7 package with Python %PYVER% %PYBITS%-bit...
    python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 8) else 1)"
    if errorlevel 1 (
        echo.
        echo ERROR: Windows 7 build must be assembled from Python 3.8.x environment.
        echo Current Python is %PYVER%.
        echo Use a dedicated legacy venv and requirements-win7.txt.
        pause
        exit /b 1
    )
)

if /I "%TARGET%"=="xp" (
    echo.
    echo ERROR: Windows XP is not supported by the current codebase and dependency stack.
    echo Need separate legacy branch, older Python toolchain, and frozen dependencies.
    pause
    exit /b 1
)

if /I not "%TARGET%"=="modern" if /I not "%TARGET%"=="win7" if /I not "%TARGET%"=="xp" (
    echo Unknown target: %TARGET%
    echo Usage:
    echo   scripts\build_exe.bat modern
    echo   scripts\build_exe.bat win7
    echo   scripts\build_exe.bat xp
    pause
    exit /b 1
)

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    if /I "%TARGET%"=="win7" (
        python -m pip install pyinstaller==5.13.2
    ) else (
        python -m pip install pyinstaller
    )
    if errorlevel 1 goto :error
)

if exist build rmdir /s /q build
if exist dist\DataFusion-RT rmdir /s /q dist\DataFusion-RT

echo Building EXE...
pyinstaller --noconfirm --clean --windowed --onedir --exclude-module multiprocessing --exclude-module _multiprocessing --exclude-module concurrent.futures.process --exclude-module pkg_resources --exclude-module setuptools --exclude-module _distutils_hack --hidden-import secrets --hidden-import openpyxl --collect-submodules openpyxl --name DataFusion-RT --distpath dist --workpath build --specpath build app\main.py
if errorlevel 1 goto :error

if not exist "dist\DataFusion-RT\config" mkdir "dist\DataFusion-RT\config"
if not exist "dist\DataFusion-RT\data" mkdir "dist\DataFusion-RT\data"
if not exist "dist\DataFusion-RT\logs" mkdir "dist\DataFusion-RT\logs"

copy /y "config\config.yaml" "dist\DataFusion-RT\config\config.yaml" >nul
copy /y "config\config.example.yaml" "dist\DataFusion-RT\config\config.example.yaml" >nul
copy /y "README.md" "dist\DataFusion-RT\README.md" >nul
if exist "BUILD_WINDOWS_COMPAT_RU.md" copy /y "BUILD_WINDOWS_COMPAT_RU.md" "dist\DataFusion-RT\BUILD_WINDOWS_COMPAT_RU.md" >nul

echo Build completed.
echo EXE: dist\DataFusion-RT\DataFusion-RT.exe
goto :end

:error
echo Build failed.
pause
exit /b 1

:end
endlocal
