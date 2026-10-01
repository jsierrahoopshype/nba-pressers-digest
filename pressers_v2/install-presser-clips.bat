@echo off
setlocal EnableExtensions
title NBA Presser Clips - installer
rem Installs NBA Presser Clips for the current Windows user: Python (if
rem missing), ffmpeg, the app, the "Clip it" link handler and a desktop
rem shortcut, then asks three setup questions. Safe to run again: it
rem updates everything and asks again.
rem No GitHub account or token needed; no administrator rights needed.
set "APP=%LOCALAPPDATA%\NBA Presser Clips"
set "RAW=https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2"
set "WINGET_ARGS=--accept-package-agreements --accept-source-agreements --disable-interactivity"
echo ============================================
echo   NBA Presser Clips - installer
echo ============================================
echo.
if not defined LOCALAPPDATA goto :no_app
where curl.exe >nul 2>nul || goto :no_curl

rem --- 1. Python 3.10 or newer ------------------------------------------------
call :find_python
if defined PY goto :have_python
echo Python was not found. Installing Python 3.12 for this user (a few minutes)...
where winget >nul 2>nul || goto :no_winget
winget install -e --id Python.Python.3.12 --scope user %WINGET_ARGS%
call :find_python
if not defined PY goto :no_python
:have_python
echo Python: %PY%
echo.

rem --- 2. The app files -------------------------------------------------------
if not exist "%APP%" mkdir "%APP%"
if not exist "%APP%" goto :no_app
echo Downloading the app into %APP% ...
for %%F in (caption_align.py reframe.py make_presser_clips.py presser_clips_setup.py run-presser-clips.bat) do (
  curl.exe -fsSL --max-time 120 -o "%APP%\%%F.new" "%RAW%/%%F"
  if errorlevel 1 goto :dl_fail
  move /y "%APP%\%%F.new" "%APP%\%%F" >nul
)
rem The face-detection model (YuNet, MIT licence) for the speaker-following crop
if not exist "%APP%\models" mkdir "%APP%\models"
curl.exe -fsSL --max-time 120 -o "%APP%\models\face_detection_yunet_2023mar.onnx.new" "%RAW%/models/face_detection_yunet_2023mar.onnx"
if errorlevel 1 goto :dl_fail
move /y "%APP%\models\face_detection_yunet_2023mar.onnx.new" "%APP%\models\face_detection_yunet_2023mar.onnx" >nul
echo.

rem --- 3. A private Python environment: yt-dlp, deno, OpenCV, faster-whisper --
set "VPY=%APP%\venv\Scripts\python.exe"
if not exist "%VPY%" "%PY%" -m venv "%APP%\venv"
if not exist "%VPY%" goto :venv_fail
echo Installing yt-dlp, deno, OpenCV and faster-whisper (the first time takes a few minutes)...
"%VPY%" -m pip install --upgrade --disable-pip-version-check --quiet pip
"%VPY%" -m pip install --upgrade --disable-pip-version-check --quiet "yt-dlp[default]" certifi deno opencv-python-headless
if errorlevel 1 goto :pip_fail
"%VPY%" -m pip install --upgrade --disable-pip-version-check --quiet faster-whisper
if errorlevel 1 echo NOTE: faster-whisper could not be installed. Videos without YouTube captions will be skipped.
echo.

rem --- 4. ffmpeg (with subtitle support) --------------------------------------
"%VPY%" "%APP%\presser_clips_setup.py" --find-ffmpeg
if not errorlevel 1 goto :have_ffmpeg
echo Installing ffmpeg...
where winget >nul 2>nul || goto :no_winget
winget install -e --id Gyan.FFmpeg %WINGET_ARGS%
"%VPY%" "%APP%\presser_clips_setup.py" --find-ffmpeg
if errorlevel 1 goto :no_ffmpeg
:have_ffmpeg
"%VPY%" "%APP%\presser_clips_setup.py" --find-deno >nul
if errorlevel 1 (
  where winget >nul 2>nul && winget install -e --id DenoLand.Deno %WINGET_ARGS%
)
echo.

rem --- 5. The three questions, Clip it links, desktop shortcut ----------------
"%VPY%" "%APP%\presser_clips_setup.py" --setup
if errorlevel 1 goto :setup_fail
goto :end

:find_python
set "PY="
for %%V in (313 312 311 310) do if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
if defined PY exit /b 0
call :probe_python py
if not defined PY call :probe_python python
exit /b 0

:probe_python
set "PYTMP=%TEMP%\nba-presser-clips-python.txt"
del "%PYTMP%" 2>nul
%1 -c "import sys; assert sys.version_info >= (3, 10); print(sys.executable)" > "%PYTMP%" 2>nul
if errorlevel 1 exit /b 0
set /p PY=<"%PYTMP%"
del "%PYTMP%" 2>nul
exit /b 0

:no_curl
echo [X] curl.exe is missing; it ships with Windows 10 (version 1803) and later. Please update Windows.
goto :end

:no_winget
echo [X] winget (Microsoft "App Installer") is missing, so Python/ffmpeg can't be installed automatically.
echo     Fix: install "App Installer" from the Microsoft Store, then run this installer again.
goto :end

:no_python
echo [X] Python could not be installed.
echo     Fix: install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo     then run this installer again.
goto :end

:no_app
echo [X] Could not create the app folder "%APP%".
goto :end

:dl_fail
echo [X] Download failed. Check your internet connection and run this installer again.
del /q "%APP%\*.new" 2>nul
goto :end

:venv_fail
echo [X] Could not create the Python environment in "%APP%\venv".
goto :end

:pip_fail
echo [X] pip could not install yt-dlp. Check your internet connection and run this installer again.
goto :end

:no_ffmpeg
echo [X] ffmpeg (with subtitle support) is still missing.
echo     Fix: close this window, open a new one and run this installer again.
echo     If that doesn't help, open PowerShell and run:  winget install -e --id Gyan.FFmpeg
goto :end

:setup_fail
echo [X] Setup did not finish. Run this installer again.
goto :end

:end
echo.
pause
endlocal
