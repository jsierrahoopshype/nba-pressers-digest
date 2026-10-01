@echo off
setlocal
rem NBA pressers PC job. Task Scheduler starts this hidden (run-hidden.vbs),
rem 30 minutes after each pressers-v2 cloud run. Double-click to run it by
rem hand and watch the output. Log: Documents\presser-clips\pc-job-log.txt
set "WORK=%USERPROFILE%\Documents\nba-pressers-digest-pc"
set "RAW=https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2"
set "LOGF=%USERPROFILE%\Documents\presser-clips\pc-job-log.txt"
cd /d "%WORK%" || exit /b 1

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :no_python

rem Self-update: latest Python files from main. A failed download keeps the
rem previous copy. This .bat itself is only updated by the installer.
for %%F in (presser_pc_job.py make_presser_clips.py caption_align.py presser_extractor.py requirements.txt) do (
  curl.exe -fsSL --max-time 60 -o "%%F.new" "%RAW%/%%F" && move /y "%%F.new" "%%F" >nul
  if exist "%%F.new" del "%%F.new"
)

rem New packages, if requirements.txt gained any (fast no-op otherwise).
%PY% -m pip install --disable-pip-version-check --quiet -r requirements.txt >nul 2>nul
%PY% presser_pc_job.py %*
exit /b %errorlevel%

:no_python
if not exist "%USERPROFILE%\Documents\presser-clips" mkdir "%USERPROFILE%\Documents\presser-clips"
>>"%LOGF%" echo [X] Python not found. Install Python 3.11+ from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
exit /b 1
