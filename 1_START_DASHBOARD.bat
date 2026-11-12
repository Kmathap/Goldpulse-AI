@echo off
title GoldPulse AI - SERVER (keep this window open)
cd /d "%~dp0"
echo Starting GoldPulse AI server... keep this window open.
echo Dashboard: http://127.0.0.1:8010
start "" http://127.0.0.1:8010
python -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8010
pause
