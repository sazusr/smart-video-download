@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Video Downloader (DEV)
py app.py --dev --open
pause
