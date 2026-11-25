@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Video Downloader

rem Already running? Just open the page.
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  start "" http://127.0.0.1:5000
  exit /b
)

echo Checking for updates (yt-dlp)...
py -m pip install -q -U "yt-dlp[default]" flask
py app.py --open %*
pause
