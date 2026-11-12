@echo off
title GoldPulse AI - Data and news check
cd /d "%~dp0"
python scripts\check_feed.py
echo.
python scripts\check_news.py
echo.
pause
