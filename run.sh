#!/usr/bin/env bash
# RunCoach AI launcher (macOS / Linux)
# Usage: ./run.sh
set -e

cd "$(dirname "$0")"

# 1) Create venv if missing
if [ ! -d ".venv" ]; then
  echo "[setup] Creating virtual environment..."
  python3 -m venv .venv
fi

# 2) Activate venv
source .venv/bin/activate

# 3) Install / upgrade deps
echo "[setup] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# 4) Warn if .env missing
if [ ! -f ".env" ]; then
  echo ""
  echo "⚠️  No .env file found."
  echo "   Copy .env.example to .env and fill in your Strava API keys."
  echo "   See README.md → 'Strava API setup' for step-by-step instructions."
  echo ""
  cp .env.example .env
  echo "[setup] Created .env from template — please edit it now."
  exit 1
fi

# 5) Launch
echo ""
echo "🏃 Starting RunCoach AI at http://localhost:8501 ..."
echo ""
streamlit run app.py
