@echo off
setlocal
title NBA Presser Clips
rem Launcher installed by install-presser-clips.bat (the desktop shortcut
rem "NBA Presser Clips" opens it). Refreshes the app from GitHub, then opens
rem the pick-list. With --auto (Task Scheduler, hidden) it runs the
rem automatic job instead.
set "APP=%~dp0"
set "VPY=%APP%venv\Scripts\python.exe"
if not exist "%VPY%" goto :not_installed
if /i "%~1"=="--auto" goto :auto
"%VPY%" "%APP%presser_clips_setup.py" --update
"%VPY%" "%APP%make_presser_clips.py" %*
echo.
pause
exit /b 0

:auto
"%VPY%" "%APP%presser_clips_setup.py" --auto
exit /b %errorlevel%

:not_installed
echo NBA Presser Clips is not installed correctly. Run install-presser-clips.bat again.
pause
exit /b 1
