@echo off
setlocal
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js 22.12 or later is required. Install it and add it to PATH.
  pause
  exit /b 1
)
node scripts\start.mjs %*
set "FOOTPRINT_EXIT=%ERRORLEVEL%"
if not "%FOOTPRINT_EXIT%"=="0" pause
exit /b %FOOTPRINT_EXIT%
