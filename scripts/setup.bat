@echo off
call "%~dp0..\start.bat" --setup %*
exit /b %ERRORLEVEL%
