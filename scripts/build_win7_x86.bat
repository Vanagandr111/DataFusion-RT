@echo off
setlocal
cd /d "%~dp0\.."

set "VENV_DIR=.venv-win7-32"
set "DIST_DIR=dist-win7-x86"
set "STAGE_DIST_DIR=dist-win7-x86-tmp"
set "BUILD_DIR=build-win7-x86"
set "ZIP_PATH=%DIST_DIR%\\DataFusion-RT-win7-x86.zip"
set "PACKAGE_DIR=%DIST_DIR%\\DataFusion-RT"
set "STAGE_PACKAGE_DIR=%STAGE_DIST_DIR%\\DataFusion-RT"

if exist "%PACKAGE_DIR%\DataFusion-RT.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$target=(Resolve-Path '%PACKAGE_DIR%\DataFusion-RT.exe').Path;" ^
      "$running=Get-CimInstance Win32_Process -Filter \"name = 'DataFusion-RT.exe'\" | Where-Object { $_.ExecutablePath -eq $target };" ^
      "if ($running) { exit 7 } else { exit 0 }"
    if errorlevel 7 (
        echo ERROR: Win7 build package is currently running.
        echo Close dist-win7-x86\DataFusion-RT\DataFusion-RT.exe and run build again.
        pause
        exit /b 1
    )
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Win7 x86 toolchain not found.
    echo Run scripts\setup_win7_x86_toolchain.bat first.
    pause
    exit /b 1
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate %VENV_DIR%
    pause
    exit /b 1
)

for /f %%i in ('python -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"') do set "PYVER=%%i"
for /f %%i in ('python -c "import struct; print(struct.calcsize('P') * 8)"') do set "PYBITS=%%i"

python -c "import sys, struct; raise SystemExit(0 if sys.version_info[:2] == (3, 8) and struct.calcsize('P') * 8 == 32 else 1)"
if errorlevel 1 (
    echo ERROR: Win7 x86 build requires Python 3.8 x86 inside %VENV_DIR%.
    echo Current interpreter: %PYVER% %PYBITS%-bit
    pause
    exit /b 1
)

if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%STAGE_PACKAGE_DIR%" rmdir /s /q "%STAGE_PACKAGE_DIR%"
if exist "%STAGE_DIST_DIR%" rmdir /s /q "%STAGE_DIST_DIR%"
if exist "%ZIP_PATH%" del /q "%ZIP_PATH%"

echo Building Win7 x86 package...
pyinstaller --noconfirm --clean --windowed --onedir --exclude-module multiprocessing --exclude-module _multiprocessing --exclude-module concurrent.futures.process --hidden-import secrets --hidden-import openpyxl --collect-submodules openpyxl --name DataFusion-RT --distpath "%STAGE_DIST_DIR%" --workpath "%BUILD_DIR%" --specpath "%BUILD_DIR%" app\main.py
if errorlevel 1 goto :error

if not exist "%STAGE_PACKAGE_DIR%\config" mkdir "%STAGE_PACKAGE_DIR%\config"
if not exist "%STAGE_PACKAGE_DIR%\data" mkdir "%STAGE_PACKAGE_DIR%\data"
if not exist "%STAGE_PACKAGE_DIR%\logs" mkdir "%STAGE_PACKAGE_DIR%\logs"

copy /y "config\config.yaml" "%STAGE_PACKAGE_DIR%\config\config.yaml" >nul
copy /y "config\config.example.yaml" "%STAGE_PACKAGE_DIR%\config\config.example.yaml" >nul
copy /y "README.md" "%STAGE_PACKAGE_DIR%\README.md" >nul
copy /y "BUILD_WINDOWS_COMPAT_RU.md" "%STAGE_PACKAGE_DIR%\BUILD_WINDOWS_COMPAT_RU.md" >nul
copy /y "RUN_FROM_FOLDER_RU.txt" "%STAGE_PACKAGE_DIR%\RUN_FROM_FOLDER_RU.txt" >nul

if exist "C:\Windows\SysWOW64\ucrtbase.dll" copy /y "C:\Windows\SysWOW64\ucrtbase.dll" "%STAGE_PACKAGE_DIR%\ucrtbase.dll" >nul
if exist "C:\Windows\SysWOW64\downlevel\api-ms-win-crt-runtime-l1-1-0.dll" (
    copy /y "C:\Windows\SysWOW64\downlevel\api-ms-win-crt-*.dll" "%STAGE_PACKAGE_DIR%\" >nul
)

echo Verifying packaged runtime...
python scripts\verify_build.py "%STAGE_PACKAGE_DIR%"
if errorlevel 1 goto :error

if exist "%PACKAGE_DIR%" (
    rmdir /s /q "%PACKAGE_DIR%"
    if exist "%PACKAGE_DIR%" (
        echo ERROR: Cannot replace old dist-win7-x86\DataFusion-RT
        echo Close running app and Explorer windows that hold this folder.
        echo New build kept here: %STAGE_PACKAGE_DIR%
        pause
        exit /b 1
    )
)

move "%STAGE_PACKAGE_DIR%" "%PACKAGE_DIR%" >nul
if errorlevel 1 (
    echo ERROR: Cannot move staged build into final dist folder.
    echo Staged build kept here: %STAGE_PACKAGE_DIR%
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%PACKAGE_DIR%\\*' -DestinationPath '%ZIP_PATH%' -Force"
if errorlevel 1 goto :error

echo Build completed.
echo EXE: %DIST_DIR%\DataFusion-RT\DataFusion-RT.exe
echo ZIP: %ZIP_PATH%
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
if exist "%STAGE_DIST_DIR%" rmdir /s /q "%STAGE_DIST_DIR%"
goto :end

:error
echo Build failed.
pause
exit /b 1

:end
endlocal
