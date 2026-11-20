@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" dedupe_video_gui.py
) else (
    py -3 dedupe_video_gui.py
)
if errorlevel 1 pause
