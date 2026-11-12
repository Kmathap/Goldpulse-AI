@echo off
title GoldPulse AI - SERVER for PC + PHONE (keep this window open)
cd /d "%~dp0"
python scripts\phone_link.py
start "" http://127.0.0.1:8010
python -m uvicorn backend.api.main:app --host 0.0.0.0 --port 8010
pause
