@echo off
cd /d "%~dp0\.."
if exist venv\Scripts\python.exe (
    venv\Scripts\python.exe scripts\backup_db.py %*
) else (
    python scripts\backup_db.py %*
)
