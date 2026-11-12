@echo off
title GoldPulse AI - Backtest
cd /d "%~dp0"
python scripts\run_backtest.py
echo.
pause
