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

python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 goto :error
)

if exist build rmdir /s /q build
if exist dist\DataFusion-RT rmdir /s /q dist\DataFusion-RT

echo Building EXE...
pyinstaller --noconfirm --clean --onedir --name DataFusion-RT --distpath dist --workpath build --specpath build app\main.py
if errorlevel 1 goto :error

if not exist "dist\DataFusion-RT\config" mkdir "dist\DataFusion-RT\config"
if not exist "dist\DataFusion-RT\data" mkdir "dist\DataFusion-RT\data"
if not exist "dist\DataFusion-RT\logs" mkdir "dist\DataFusion-RT\logs"

copy /y "config\config.yaml" "dist\DataFusion-RT\config\config.yaml" >nul
copy /y "config\config.example.yaml" "dist\DataFusion-RT\config\config.example.yaml" >nul
copy /y "README.md" "dist\DataFusion-RT\README.md" >nul

echo Build completed.
echo EXE: dist\DataFusion-RT\DataFusion-RT.exe
goto :end

:error
echo Build failed.
pause
exit /b 1

:end
endlocal
