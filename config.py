"""
Configuration & constants for RunCoach AI.
- Locally: reads from .env (python-dotenv).
- On Streamlit Cloud: reads from st.secrets, falls back to env vars.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (no-op on Streamlit Cloud where .env doesn't exist)
ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def _secret(key: str, default: str = "") -> str:
    """
    Look up a config value in priority order:
      1. Streamlit secrets (st.secrets[key]) — used on Streamlit Cloud
      2. Environment variable / .env file — used locally
      3. The provided default
    """
    try:
        import streamlit as st  # lazy import so non-Streamlit scripts still work
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)


DB_PATH = ROOT / "data" / "runcoach.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---- Database URL ----
# Kalau DATABASE_URL diset (di Streamlit Cloud secrets atau env), pakai itu
# (Postgres / external SQLite). Kalau tidak, fallback ke SQLite lokal.
# Format Postgres: postgresql://user:pass@host:port/dbname
DATABASE_URL = _secret("DATABASE_URL", f"sqlite:///{DB_PATH}")

# ---- Strava API ----
STRAVA_CLIENT_ID = _secret("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = _secret("STRAVA_CLIENT_SECRET")
STRAVA_REDIRECT_URI = _secret("STRAVA_REDIRECT_URI", "http://localhost:8501")
STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_SCOPES = "read,activity:read_all,profile:read_all"

# ---- App password (only used in cloud deployment) ----
# Leave empty for no password (local use). Set in Streamlit Cloud secrets.
APP_PASSWORD = _secret("APP_PASSWORD", "")

# ---- Default athlete profile ----
DEFAULT_PROFILE = {
    "name": _secret("ATHLETE_NAME", "Runner"),
    "age": int(_secret("ATHLETE_AGE", "30")),
    "resting_hr": int(_secret("ATHLETE_RESTING_HR", "60")),
    "max_hr": int(_secret("ATHLETE_MAX_HR", "190")),
    "fthr": int(_secret("ATHLETE_FTHR", "170")),
    "threshold_pace_min_per_km": float(_secret("ATHLETE_THRESHOLD_PACE", "5.0")),
}

# ---- Coach knobs ----
CTL_TIME_CONSTANT = 42   # days — Chronic Training Load (fitness)
ATL_TIME_CONSTANT = 7    # days — Acute Training Load (fatigue)

# HR Zone definitions (% of FTHR, Friel running zones)
HR_ZONES = [
    ("Z1 Recovery",        0.00, 0.85),
    ("Z2 Aerobic",         0.85, 0.89),
    ("Z3 Tempo",           0.89, 0.94),
    ("Z4 Threshold",       0.94, 1.00),
    ("Z5a VO2 Sub",        1.00, 1.03),
    ("Z5b VO2 Max",        1.03, 1.06),
    ("Z5c Anaerobic",      1.06, 2.00),
]

# Pace zones (multiplier of threshold pace; lower = faster)
PACE_ZONES = [
    ("Z1 Easy",            1.29, 99.0),
    ("Z2 Marathon",        1.14, 1.29),
    ("Z3 Tempo",           1.06, 1.14),
    ("Z4 Threshold",       1.00, 1.06),
    ("Z5 VO2 Max",         0.90, 1.00),
    ("Z6 Anaerobic",       0.00, 0.90),
]

# Plain-language zone descriptions for the dashboard
HR_ZONE_INFO = {
    "Z1 Recovery":    ("Sangat ringan", "Ngobrol panjang lebar gampang. Recovery, warmup, cooldown."),
    "Z2 Aerobic":     ("Easy / aerobic", "Conversational — bisa ngobrol kalimat lengkap. Zona base building paling penting."),
    "Z3 Tempo":       ("Moderate / tempo", "Ngobrol pendek (3-5 kata). Steady tempo run."),
    "Z4 Threshold":   ("Comfortably hard", "1-2 kata aja. Tempo run, cruise interval, ~10K race effort."),
    "Z5a VO2 Sub":    ("Hard", "Cuma bisa napas. Interval 3-8 menit (5K pace)."),
    "Z5b VO2 Max":    ("Very hard", "VO2 max work. Interval 30 detik - 3 menit."),
    "Z5c Anaerobic":  ("All-out", "Sprint, short reps 10-30 detik."),
}

PACE_ZONE_INFO = {
    "Z1 Easy":        ("Easy / recovery", "Pace easy run — yang terasa terlalu lambat. Inti aerobic base."),
    "Z2 Marathon":    ("Marathon pace",   "Pace yang bisa Anda pertahankan untuk full marathon."),
    "Z3 Tempo":       ("Tempo",            "Pace half-marathon ke 10K. \"Comfortably hard\"."),
    "Z4 Threshold":   ("Threshold",        "Pace ~10K race. Tempo work, cruise interval."),
    "Z5 VO2 Max":     ("VO₂ Max",          "Pace ~5K race. Interval 3-5 menit."),
    "Z6 Anaerobic":   ("Anaerobic",        "Lebih cepat dari 5K race. Sprint, short reps."),
}
