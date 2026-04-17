@echo off
setlocal
cd /d "%~dp0\.."

echo Alias script.
echo Real target: Win7 x86 toolchain.
call scripts\setup_win7_x86_toolchain.bat

endlocal
