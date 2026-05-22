@echo off
REM RunCoach AI launcher (Windows)
REM Usage: double-click run.bat OR run from cmd

cd /d "%~dp0"

REM 1) Create venv if missing
if not exist ".venv" (
    echo [setup] Creating virtual environment...
    python -m venv .venv
)

REM 2) Activate venv
call .venv\Scripts\activate.bat

REM 3) Install deps
echo [setup] Installing dependencies...
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

REM 4) Warn if .env missing
if not exist ".env" (
    echo.
    echo [!] No .env file found.
    echo     Copy .env.example to .env and fill in your Strava API keys.
    echo     See README.md - "Strava API setup" for step-by-step instructions.
    echo.
    copy .env.example .env
    echo [setup] Created .env from template - please edit it now.
    pause
    exit /b 1
)

REM 5) Launch
echo.
echo Starting RunCoach AI at http://localhost:8501 ...
echo.
streamlit run app.py
pause
