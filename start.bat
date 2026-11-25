@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Video Downloader
echo Checking for updates (yt-dlp)...
py -m pip install -q -U "yt-dlp[default]" flask
py app.py --open %*
pause
