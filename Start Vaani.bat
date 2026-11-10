@echo off
title Vaani - keep this window open
cd /d "%~dp0"
echo Starting Vaani... (first time may take a minute)
python -m pip install -q --disable-pip-version-check -r requirements.txt
python server.py
echo.
echo Vaani stopped. If you see an error above, send a screenshot of this window.
pause
