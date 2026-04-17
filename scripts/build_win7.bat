@echo off
setlocal
cd /d "%~dp0\.."

echo Building Windows 7 package...
echo Alias script. Real target: Win7 x86 compatibility build.
call scripts\build_win7_x86.bat

endlocal
