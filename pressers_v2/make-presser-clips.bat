@echo off
setlocal
title NBA presser clip maker
cd /d "%~dp0"
echo ============================================
echo   NBA presser clip maker
echo ============================================
echo.

rem --- Find Python -----------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto :no_python

rem --- Install / upgrade yt-dlp ---------------------------------------------
echo Installing / updating yt-dlp...
%PY% -m pip install --upgrade --disable-pip-version-check --quiet "yt-dlp[default]"
if errorlevel 1 %PY% -m pip install --upgrade --user --disable-pip-version-check --quiet "yt-dlp[default]"
if errorlevel 1 goto :no_ytdlp
for /f "delims=" %%v in ('%PY% -m yt_dlp --version') do echo yt-dlp version %%v

rem --- faster-whisper: finds the quote when a video has no captions ---------
echo Installing / updating faster-whisper (first time can take a few minutes)...
%PY% -m pip install --upgrade --disable-pip-version-check --quiet faster-whisper
if errorlevel 1 %PY% -m pip install --upgrade --user --disable-pip-version-check --quiet faster-whisper
if errorlevel 1 goto :no_whisper
:whisper_done
echo.

rem --- The clipper needs caption_align.py next to it -------------------------
if not exist "%~dp0caption_align.py" goto :no_align

rem --- Check ffmpeg ----------------------------------------------------------
where ffmpeg >nul 2>nul
if errorlevel 1 goto :no_ffmpeg

rem --- Optional: Deno helps yt-dlp with YouTube ------------------------------
where deno >nul 2>nul
if errorlevel 1 (
  echo NOTE: Deno is not installed. If YouTube downloads fail, run this once in PowerShell:
  echo       winget install --id DenoLand.Deno -e
  echo.
)

rem --- Make the clips --------------------------------------------------------
%PY% "%~dp0make_presser_clips.py" %*
goto :end

:no_python
echo [X] Python was not found.
echo     Fix: install Python 3.11 or newer from https://www.python.org/downloads/
echo     and tick "Add python.exe to PATH" during install. Then run this file again.
goto :end

:no_ytdlp
echo [X] Could not install yt-dlp with pip. Check your internet connection and run this file again.
goto :end

:no_whisper
echo NOTE: faster-whisper could not be installed. Clips still work for videos with
echo       YouTube captions; videos without captions will be skipped and listed.
goto :whisper_done

:no_align
echo [X] caption_align.py is missing.
echo     Fix: download caption_align.py from the same GitHub folder as this file
echo     (pressers_v2/caption_align.py) and put it next to make-presser-clips.bat.
goto :end

:no_ffmpeg
echo [X] ffmpeg is missing. The clips cannot be made without it.
echo.
echo     Fix: open PowerShell and run exactly:
echo.
echo         winget install --id Gyan.FFmpeg -e
echo.
echo     When it finishes, CLOSE this window and double-click make-presser-clips.bat again.
echo     (A new window is needed so Windows picks up the updated PATH.)
goto :end

:end
echo.
pause
endlocal
