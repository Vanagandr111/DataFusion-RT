@echo off
setlocal
cd /d "%~dp0\.."

echo Windows XP build is not available in the current project branch.
echo Reason:
echo - current GUI stack and dependencies are too new
echo - XP needs separate legacy branch and pinned old libraries
echo See BUILD_WINDOWS_COMPAT_RU.md
pause

endlocal
