# 🏃 RunCoach AI

A local web app that auto-syncs your running data from **Strava** (Garmin watches that auto-upload to Strava work too!) and gives you **coach-level analytics**:

- **Training Load (PMC)** — CTL fitness, ATL fatigue, TSB form, Coggan-style
- **Pace & HR Zone Analysis** — where your time actually goes (80/20 sanity check)
- **Race Predictor** — Riegel + Daniels VDOT for 5K / 10K / Half / Marathon
- **AI Coach** — daily workout recommendation based on your form & ramp rate
- **Recovery Time** — hours-to-ready after each session
- **Per-activity deep dive** — pace, HR, elevation, zone breakdown

Runs 100% locally on your computer. Your data stays in `data/runcoach.db` on your machine. No cloud, no subscription.

---

## ✅ Quick start (TL;DR for runners, not coders)

1. **Install Python 3.10+** if you don't have it — [python.org/downloads](https://www.python.org/downloads/) (on macOS it's usually already there).
2. **Get free Strava API keys** (5 minutes, see step-by-step below).
3. Fill them in `.env`.
4. **macOS / Linux:** double-click `run.sh` (or run `./run.sh` in Terminal).
   **Windows:** double-click `run.bat`.
5. Browser opens at `http://localhost:8501` → click **Connect Strava** in the sidebar → authorize → click **Sync activities**.

That's it. Refresh whenever you want — the sidebar **Sync** button pulls only new activities.

---

## 🔑 Strava API setup (one-time, 5 min)

You need your own free Strava "API application" to let the app read your activities. This is standard practice — Strava gives every user 100 free apps.

1. Go to **https://www.strava.com/settings/api**
2. If you've never made an app before, you'll be asked to create one:
   - **Application Name:** `RunCoach AI` (or anything)
   - **Category:** `Data Importer`
   - **Website:** `http://localhost`
   - **Authorization Callback Domain:** `localhost`  ← **must be exactly this**
   - Upload any icon (or skip).
3. After creating, you'll see:
   - **Client ID** — short number
   - **Client Secret** — long string (click "Show" to reveal)
4. In this project folder, copy the template:
   ```bash
   cp .env.example .env
   ```
   Open `.env` in any text editor and paste your values:
   ```
   STRAVA_CLIENT_ID=12345
   STRAVA_CLIENT_SECRET=abcdef0123456789...
   STRAVA_REDIRECT_URI=http://localhost:8501
   ```
5. Save the file. Done!

> **Note:** The first time you click **Connect Strava** in the app, Strava asks you to authorize. Make sure to **check the "View data about your activities" box** — otherwise the app can't read your runs.

---

## 🏃 What about Garmin?

Garmin Connect doesn't have a free, open API for personal use. **The easy path is to connect Garmin → Strava once** (in your Garmin Connect app: Settings → Partner Connections → Strava). After that, every Garmin run auto-syncs to Strava, and this app syncs from Strava. You get the full Garmin data fidelity (HR, cadence, elevation, GPS) without any Garmin API hassle.

If you really want to skip Strava and go direct to Garmin: that requires either a Garmin partnership (paid) or unofficial libraries like `garminconnect` (Python) which break often. Not recommended for newbies.

---

## 📁 Project layout

```
runcoach/
├── app.py              # Streamlit UI (the dashboard you see in your browser)
├── analytics.py        # Coach math: TSS, CTL/ATL/TSB, VDOT, zones, recovery, advice
├── strava_client.py    # OAuth + Strava API calls
├── database.py         # SQLite layer (activities, streams, profile, tokens)
├── config.py           # Constants & athlete profile defaults
├── requirements.txt    # Python dependencies
├── .env.example        # Template for your API keys
├── .env                # Your actual keys (you create this, never commit!)
├── run.sh / run.bat    # One-click launchers
├── data/runcoach.db    # Auto-created SQLite database (your data)
└── .venv/              # Auto-created Python virtual env
```

---

## 🧠 How the coach math works

| Metric | Formula | What it tells you |
|---|---|---|
| **TSS** | HR-based TRIMP if HR available, else pace rTSS = IF² × hours × 100 | Stress per workout. 100 = 1h all-out at threshold. |
| **IF** | Threshold pace ÷ actual pace | Intensity. 1.0 = threshold; >1.0 = harder than threshold. |
| **CTL** | 42-day exponential moving avg of daily TSS | Long-term fitness. |
| **ATL** | 7-day exponential moving avg of daily TSS | Short-term fatigue. |
| **TSB** | CTL − ATL | Form / freshness. +5..+25 = race-ready. |
| **VDOT** | Daniels' Running Formula | VO₂max estimate from race-like efforts. |
| **Race time** | Riegel: T₂ = T₁·(D₂/D₁)^1.06 | Predict 5K/10K/Half/Marathon from best recent effort. |
| **Recovery hrs** | TSS·0.5 × intensity multiplier | Hours until you're ready for another hard session. |
| **Daily advice** | Rule-based on TSB & 7-day CTL ramp | What to do today. |

All formulas are widely-published sport-science references (Coggan, Daniels, Friel, Banister). See in-code comments for citations.

---

## 🛠 Troubleshooting

- **"streamlit: command not found"** → The venv didn't activate. Re-run `./run.sh` or `run.bat`.
- **"Strava API keys missing"** → `.env` doesn't exist or is empty. See Strava API setup above.
- **"OAuth failed" / "redirect_uri mismatch"** → In your Strava API settings, **Authorization Callback Domain** must be exactly `localhost` (no http://, no port, no path).
- **Sync says rate-limit** → Strava limits to 100 calls / 15 min. Wait 15 min, sync again.
- **No HR zones showing** → Either your watch didn't record HR, or you haven't set your **FTHR** in Settings.
- **Race predictor empty** → Need at least one run ≥ 5 km in the last 90 days.

---

## 🔒 Privacy

- Your `.env` (API keys) and `data/runcoach.db` (activities) **never leave your computer**.
- The app only talks to `strava.com` to fetch your data.
- Don't share your `.env` or commit it to git — that's your key, treat it like a password.

---

## 📜 License

MIT — do whatever you want, but no warranty. This is a personal training tool, not medical advice. Listen to your body, and consult a real coach or doctor for serious training decisions.

Happy running! 🏃💨
