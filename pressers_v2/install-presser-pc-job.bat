@echo off
setlocal
title Install NBA presser PC job
rem One-time setup for the unattended PC job. Safe to run again (updates files,
rem keeps your token unless you choose to replace it, re-registers the task).
set "WORK=%USERPROFILE%\Documents\nba-pressers-digest-pc"
set "RAW=https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2"
set "TASK=NBA Pressers PC Job"
echo ============================================
echo   NBA presser PC job - installer
echo ============================================
echo.

rem --- Python -------------------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :no_python

rem --- Download the job files ---------------------------------------------------
where curl.exe >nul 2>nul || goto :no_curl
if not exist "%WORK%" mkdir "%WORK%"
cd /d "%WORK%" || goto :no_work
echo Downloading the job files into %WORK% ...
for %%F in (presser_pc_job.py make_presser_clips.py caption_align.py presser_extractor.py requirements.txt run-presser-pc-job.bat) do (
  curl.exe -fsSL --max-time 60 -o "%%F.new" "%RAW%/%%F"
  if errorlevel 1 goto :dl_fail
  move /y "%%F.new" "%%F" >nul
)
echo.

rem --- Python packages ------------------------------------------------------------
echo Installing Python packages: pipeline requirements, yt-dlp, faster-whisper.
echo (The first time can take a few minutes.)
%PY% -m pip install --upgrade --disable-pip-version-check --quiet -r requirements.txt "yt-dlp[default]" faster-whisper
if errorlevel 1 %PY% -m pip install --upgrade --user --disable-pip-version-check --quiet -r requirements.txt "yt-dlp[default]" faster-whisper
if errorlevel 1 goto :pip_fail
echo.

rem --- ffmpeg, Deno, wscript ---------------------------------------------------------
where ffmpeg >nul 2>nul || goto :no_ffmpeg
where deno >nul 2>nul || (
  echo NOTE: Deno is not installed. If YouTube downloads fail later, run this once in PowerShell:
  echo       winget install --id DenoLand.Deno -e
  echo.
)
where wscript.exe >nul 2>nul || goto :no_wscript

rem --- GitHub token (stored outside the repo folder) ------------------------------------
if exist "%USERPROFILE%\.nba-pressers\token" (
  choice /c KR /m "A GitHub token is already saved. K = keep it, R = replace it"
  if errorlevel 2 goto :ask_token
  goto :token_ok
)
:ask_token
echo.
echo Create the token at https://github.com/settings/personal-access-tokens/new
echo   - Repository access: Only select repositories, nba-pressers-digest
echo   - Permissions: Contents = Read and write, Actions = Read-only
echo.
%PY% presser_pc_job.py --setup-token
if errorlevel 1 goto :token_fail
:token_ok
echo.

rem --- Task Scheduler ------------------------------------------------------------------
%PY% presser_pc_job.py --write-task-xml "%WORK%\task.xml" --vbs "%WORK%\run-hidden.vbs"
if errorlevel 1 goto :task_fail
schtasks /create /tn "%TASK%" /xml "%WORK%\task.xml" /f
if errorlevel 1 goto :task_fail
echo.
echo Installed. The job runs by itself (hidden) when the PC is on and you are logged in.
echo Clips: %USERPROFILE%\Documents\presser-clips\
echo Log:   %USERPROFILE%\Documents\presser-clips\pc-job-log.txt
echo.
choice /c YN /m "Run the job once now to test it"
if errorlevel 2 goto :end
call "%WORK%\run-presser-pc-job.bat"
goto :end

:no_python
echo [X] Python was not found.
echo     Fix: install Python 3.11 or newer from https://www.python.org/downloads/
echo     and tick "Add python.exe to PATH". Then run this installer again.
goto :end

:no_curl
echo [X] curl.exe is missing; it ships with Windows 10 1803 and later. Please update Windows.
goto :end

:no_work
echo [X] Could not create %WORK%
goto :end

:dl_fail
echo [X] Download failed. Check your internet connection and run this installer again.
del /q "%WORK%\*.new" 2>nul
goto :end

:pip_fail
echo [X] pip could not install the Python packages. Check your internet connection and try again.
goto :end

:no_ffmpeg
echo [X] ffmpeg is missing.
echo.
echo     Fix: open PowerShell and run exactly:
echo.
echo         winget install --id Gyan.FFmpeg -e
echo.
echo     Then CLOSE this window and run install-presser-pc-job.bat again.
goto :end

:no_wscript
echo [X] Windows Script Host (wscript.exe) is missing; it runs the job without a window.
echo     Fix: Settings, System, Optional features, View features, add "VBSCRIPT", then run this again.
goto :end

:token_fail
echo [X] The token was not saved. Run this installer again and paste a valid token.
goto :end

:task_fail
echo [X] Could not register the scheduled task "%TASK%".
goto :end

:end
echo.
pause
endlocal
