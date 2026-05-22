"""
SQLite database layer for RunCoach AI — MULTI-USER.

Setiap data (tokens, profile, activities, training_plans) di-scope ke
`user_id`, yang sama dengan **Strava athlete_id** (stable & unique per akun
Strava). Jadi gak perlu login/password — connect Strava = otomatis "login".

Setiap fungsi yang menyentuh data per-user butuh user_id sebagai
argumen pertama. Streams disimpan per activity_id (tidak butuh user_id
karena activity sudah punya user_id-nya).
"""
import sqlite3
import json
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List, Dict, Any
import pandas as pd

from config import DB_PATH


SCHEMA = """
-- Master user table — populated lewat OAuth callback
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    athlete_json TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    user_id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    athlete_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    user_id INTEGER PRIMARY KEY,
    name TEXT,
    age INTEGER,
    resting_hr INTEGER,
    max_hr INTEGER,
    fthr INTEGER,
    threshold_pace_min_per_km REAL,
    weight_kg REAL,
    goal_race TEXT,
    goal_time TEXT,
    goal_date TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    user_id INTEGER NOT NULL,
    id INTEGER NOT NULL,
    name TEXT,
    sport_type TEXT,
    start_date TEXT,
    start_date_local TEXT,
    timezone TEXT,
    distance_m REAL,
    moving_time_s INTEGER,
    elapsed_time_s INTEGER,
    total_elevation_gain_m REAL,
    average_speed_mps REAL,
    max_speed_mps REAL,
    average_heartrate REAL,
    max_heartrate REAL,
    average_cadence REAL,
    suffer_score REAL,
    kudos_count INTEGER,
    has_heartrate INTEGER,
    tss REAL,
    intensity_factor REAL,
    normalized_pace_min_per_km REAL,
    average_pace_min_per_km REAL,
    estimated_vo2max REAL,
    raw_json TEXT,
    PRIMARY KEY (user_id, id)
);

CREATE TABLE IF NOT EXISTS streams (
    activity_id INTEGER PRIMARY KEY,
    time_json TEXT,
    distance_json TEXT,
    heartrate_json TEXT,
    velocity_smooth_json TEXT,
    cadence_json TEXT,
    altitude_json TEXT,
    fetched_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_activities_user_date ON activities(user_id, start_date);
CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport_type);

CREATE TABLE IF NOT EXISTS training_plans (
    user_id INTEGER PRIMARY KEY,
    race_type TEXT NOT NULL,
    race_distance_km REAL NOT NULL,
    race_date TEXT NOT NULL,
    target_time_s INTEGER NOT NULL,
    target_pace_min_per_km REAL,
    plan_start_date TEXT NOT NULL,
    plan_weeks INTEGER NOT NULL,
    runs_per_week INTEGER NOT NULL,
    peak_weekly_km REAL,
    current_weekly_km REAL,
    current_ctl REAL,
    created_at TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS planned_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    week_num INTEGER NOT NULL,
    phase TEXT NOT NULL,
    session_type TEXT NOT NULL,
    distance_km REAL,
    duration_min REAL,
    target_pace_min_per_km REAL,
    target_hr_zone TEXT,
    description TEXT,
    workout_detail TEXT,
    is_quality INTEGER,
    executed_activity_id INTEGER,
    UNIQUE(user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_planned_sessions_user_date ON planned_sessions(user_id, date);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _legacy_table_is_single_user(c, table: str) -> bool:
    """
    Cek apakah tabel `table` masih pakai schema lama (PRIMARY KEY id=1).
    Kalau iya, kita perlu migrate ke schema baru per-user.
    """
    try:
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        return "user_id" not in cols
    except sqlite3.OperationalError:
        return False


def _migrate_legacy_single_user_db():
    """
    Migrate DB lama (single-user, id=1 everywhere) ke schema multi-user.
    Strategi: rename tabel lama, bikin schema baru, copy data ke tabel baru
    pakai athlete_id dari tokens table sebagai user_id.
    """
    if not os.path.exists(DB_PATH):
        return  # DB belum dibuat, skip

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Cek apakah perlu migrate (tabel tokens lama tidak punya user_id)
        try:
            tokens_cols = [r["name"] for r in conn.execute("PRAGMA table_info(tokens)").fetchall()]
        except sqlite3.OperationalError:
            tokens_cols = []
        if not tokens_cols or "user_id" in tokens_cols:
            return  # Schema sudah baru atau belum ada

        # Ambil athlete_id dari row lama
        try:
            row = conn.execute("SELECT * FROM tokens WHERE id = 1").fetchone()
        except sqlite3.OperationalError:
            row = None
        if not row:
            # Tabel lama ada tapi kosong — drop dan recreate
            for t in ["tokens", "profile", "activities", "training_plans",
                      "planned_sessions", "streams"]:
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {t}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()
            return

        legacy_user_id = row["athlete_id"]
        if not legacy_user_id:
            # Gak ada athlete_id — coba ekstrak dari athlete_json
            try:
                aj = row["athlete_json"]
                if aj:
                    import json as _j
                    parsed = _j.loads(aj)
                    legacy_user_id = parsed.get("id")
            except Exception:
                pass
        if not legacy_user_id:
            # Masih gak ada — pakai user_id=0 sebagai placeholder, akan di-claim
            # saat user OAuth pertama kali. Lebih baik daripada drop semua data.
            legacy_user_id = 0

        # --- Rename tabel lama jadi _legacy ---
        for t in ["tokens", "profile", "activities", "training_plans", "planned_sessions"]:
            try:
                conn.execute(f"ALTER TABLE {t} RENAME TO {t}_legacy")
            except sqlite3.OperationalError:
                pass
        conn.commit()

        # --- Bikin schema baru ---
        conn.executescript(SCHEMA)

        now = datetime.utcnow().isoformat()

        # users
        athlete_json = row["athlete_json"]
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, athlete_json, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            (legacy_user_id, athlete_json, now, now),
        )

        # tokens
        conn.execute(
            """INSERT INTO tokens (user_id, access_token, refresh_token, expires_at, athlete_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (legacy_user_id, row["access_token"], row["refresh_token"],
             row["expires_at"], athlete_json, row["updated_at"]),
        )

        # profile
        try:
            prow = conn.execute("SELECT * FROM profile_legacy WHERE id = 1").fetchone()
        except sqlite3.OperationalError:
            prow = None
        if prow:
            conn.execute(
                """INSERT INTO profile
                   (user_id, name, age, resting_hr, max_hr, fthr,
                    threshold_pace_min_per_km, weight_kg, goal_race,
                    goal_time, goal_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (legacy_user_id, prow["name"], prow["age"], prow["resting_hr"],
                 prow["max_hr"], prow["fthr"], prow["threshold_pace_min_per_km"],
                 prow["weight_kg"], prow["goal_race"], prow["goal_time"],
                 prow["goal_date"]),
            )

        # activities — copy semua dengan user_id ditambahkan
        try:
            arows = conn.execute("SELECT * FROM activities_legacy").fetchall()
        except sqlite3.OperationalError:
            arows = []
        for a in arows:
            cols = list(a.keys())
            vals = [a[c] for c in cols]
            placeholders = ",".join("?" * (len(cols) + 1))
            col_list = "user_id," + ",".join(cols)
            conn.execute(
                f"INSERT OR REPLACE INTO activities ({col_list}) VALUES ({placeholders})",
                [legacy_user_id] + vals,
            )

        # training_plans
        try:
            tp = conn.execute("SELECT * FROM training_plans_legacy WHERE id = 1").fetchone()
        except sqlite3.OperationalError:
            tp = None
        if tp:
            conn.execute(
                """INSERT INTO training_plans
                   (user_id, race_type, race_distance_km, race_date, target_time_s,
                    target_pace_min_per_km, plan_start_date, plan_weeks,
                    runs_per_week, peak_weekly_km, current_weekly_km,
                    current_ctl, created_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (legacy_user_id, tp["race_type"], tp["race_distance_km"],
                 tp["race_date"], tp["target_time_s"],
                 tp["target_pace_min_per_km"], tp["plan_start_date"],
                 tp["plan_weeks"], tp["runs_per_week"], tp["peak_weekly_km"],
                 tp["current_weekly_km"], tp["current_ctl"],
                 tp["created_at"], tp["notes"]),
            )

        # planned_sessions
        try:
            ps_rows = conn.execute("SELECT * FROM planned_sessions_legacy").fetchall()
        except sqlite3.OperationalError:
            ps_rows = []
        for s in ps_rows:
            conn.execute(
                """INSERT OR REPLACE INTO planned_sessions
                   (user_id, date, week_num, phase, session_type,
                    distance_km, duration_min, target_pace_min_per_km,
                    target_hr_zone, description, workout_detail, is_quality,
                    executed_activity_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (legacy_user_id, s["date"], s["week_num"], s["phase"],
                 s["session_type"], s["distance_km"], s["duration_min"],
                 s["target_pace_min_per_km"], s["target_hr_zone"],
                 s["description"], s["workout_detail"], s["is_quality"],
                 s["executed_activity_id"]),
            )

        # --- Drop _legacy tables ---
        for t in ["tokens_legacy", "profile_legacy", "activities_legacy",
                  "training_plans_legacy", "planned_sessions_legacy"]:
            try:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            except sqlite3.OperationalError:
                pass

        conn.commit()
    finally:
        conn.close()


def init_db():
    # Run migration first (no-op kalau sudah multi-user)
    _migrate_legacy_single_user_db()
    with get_conn() as c:
        c.executescript(SCHEMA)


# ---------- Users ----------
def upsert_user(user_id: int, athlete: Optional[dict] = None):
    """Catat user lewat OAuth. Idempotent."""
    now = datetime.utcnow().isoformat()
    with get_conn() as c:
        existing = c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            c.execute(
                "UPDATE users SET athlete_json = COALESCE(?, athlete_json), last_seen = ? WHERE user_id = ?",
                (json.dumps(athlete) if athlete else None, now, user_id),
            )
        else:
            c.execute(
                "INSERT INTO users (user_id, athlete_json, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (user_id, json.dumps(athlete) if athlete else None, now, now),
            )


def get_user(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users() -> List[dict]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE user_id > 0 ORDER BY last_seen DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def has_orphan_data() -> bool:
    """Cek apakah ada data legacy dengan user_id=0 (placeholder dari migration lama)."""
    with get_conn() as c:
        try:
            n = c.execute("SELECT COUNT(*) FROM activities WHERE user_id = 0").fetchone()[0]
            if n > 0:
                return True
            n = c.execute("SELECT COUNT(*) FROM training_plans WHERE user_id = 0").fetchone()[0]
            return n > 0
        except sqlite3.OperationalError:
            return False


def claim_orphan_data(new_user_id: int) -> dict:
    """
    Transfer semua data dengan user_id=0 ke `new_user_id`. Dipanggil saat
    user OAuth pertama kali setelah migrasi dari schema single-user.
    Return: dict berisi jumlah row yang dipindah per tabel.
    """
    moved = {}
    with get_conn() as c:
        for table in ["activities", "training_plans", "planned_sessions", "profile"]:
            try:
                # Hapus data baru kalau sudah ada (avoid duplicate key violation)
                if table == "profile":
                    c.execute("DELETE FROM profile WHERE user_id = ?", (new_user_id,))
                elif table == "training_plans":
                    c.execute("DELETE FROM training_plans WHERE user_id = ?", (new_user_id,))
                # Pindahkan row dari user_id=0 ke new_user_id
                cur = c.execute(
                    f"UPDATE {table} SET user_id = ? WHERE user_id = 0",
                    (new_user_id,),
                )
                moved[table] = cur.rowcount
            except sqlite3.OperationalError as e:
                moved[table] = f"error: {e}"
        # Hapus user_id=0 placeholder dari users + tokens table.
        # Token baru sudah ditulis oleh exchange_code_for_token() di OAuth callback.
        try:
            c.execute("DELETE FROM tokens WHERE user_id = 0")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("DELETE FROM users WHERE user_id = 0")
        except sqlite3.OperationalError:
            pass
    return moved


# ---------- Tokens ----------
def save_tokens(user_id: int, access: str, refresh: str, expires_at: int,
                athlete: Optional[dict] = None):
    """Simpan/refresh token untuk user. user_id = Strava athlete_id."""
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO tokens
               (user_id, access_token, refresh_token, expires_at, athlete_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, access, refresh, expires_at,
             json.dumps(athlete) if athlete else None,
             datetime.utcnow().isoformat()),
        )


def get_tokens(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute("SELECT * FROM tokens WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def clear_tokens(user_id: int):
    with get_conn() as c:
        c.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))


# ---------- Profile ----------
def save_profile(user_id: int, p: dict):
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO profile
               (user_id, name, age, resting_hr, max_hr, fthr,
                threshold_pace_min_per_km, weight_kg, goal_race,
                goal_time, goal_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, p.get("name"), p.get("age"), p.get("resting_hr"),
             p.get("max_hr"), p.get("fthr"), p.get("threshold_pace_min_per_km"),
             p.get("weight_kg"), p.get("goal_race"),
             p.get("goal_time"), p.get("goal_date")),
        )


def get_profile(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute("SELECT * FROM profile WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


# ---------- Activities ----------
def upsert_activity(user_id: int, act: Dict[str, Any]):
    """Insert/replace activity Strava untuk user tertentu."""
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO activities
               (user_id, id, name, sport_type, start_date, start_date_local, timezone,
                distance_m, moving_time_s, elapsed_time_s, total_elevation_gain_m,
                average_speed_mps, max_speed_mps, average_heartrate, max_heartrate,
                average_cadence, suffer_score, kudos_count, has_heartrate, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, act.get("id"), act.get("name"),
             act.get("sport_type") or act.get("type"),
             act.get("start_date"), act.get("start_date_local"), act.get("timezone"),
             act.get("distance"), act.get("moving_time"), act.get("elapsed_time"),
             act.get("total_elevation_gain"),
             act.get("average_speed"), act.get("max_speed"),
             act.get("average_heartrate"), act.get("max_heartrate"),
             act.get("average_cadence"), act.get("suffer_score"),
             act.get("kudos_count"),
             1 if act.get("has_heartrate") else 0,
             json.dumps(act)),
        )


def update_activity_metrics(user_id: int, activity_id: int, metrics: Dict[str, Any]):
    """Update derived coach metrics on an existing activity."""
    fields = ["tss", "intensity_factor", "normalized_pace_min_per_km",
              "average_pace_min_per_km", "estimated_vo2max"]
    sets = ", ".join(f"{f} = ?" for f in fields)
    values = [metrics.get(f) for f in fields] + [user_id, activity_id]
    with get_conn() as c:
        c.execute(
            f"UPDATE activities SET {sets} WHERE user_id = ? AND id = ?",
            values,
        )


def get_latest_activity_date(user_id: int) -> Optional[str]:
    with get_conn() as c:
        row = c.execute(
            "SELECT MAX(start_date) AS last FROM activities WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["last"] if row and row["last"] else None


def get_activities_df(user_id: int, sport_filter: Optional[str] = "Run") -> pd.DataFrame:
    with get_conn() as c:
        q = "SELECT * FROM activities WHERE user_id = ?"
        params: list = [user_id]
        if sport_filter:
            q += " AND sport_type LIKE ?"
            params.append(f"%{sport_filter}%")
        q += " ORDER BY start_date DESC"
        df = pd.read_sql_query(q, c, params=params)
    if not df.empty:
        df["start_date"] = pd.to_datetime(df["start_date"], utc=True, errors="coerce")
        df["start_date_local"] = pd.to_datetime(df["start_date_local"], errors="coerce")
        df["distance_km"] = df["distance_m"] / 1000.0
        df["moving_time_min"] = df["moving_time_s"] / 60.0
        df["pace_min_per_km"] = df.apply(
            lambda r: (r["moving_time_s"] / 60.0) / (r["distance_m"] / 1000.0)
            if r["distance_m"] and r["distance_m"] > 0 else None,
            axis=1,
        )
    return df


def get_activity(user_id: int, activity_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM activities WHERE user_id = ? AND id = ?",
            (user_id, activity_id),
        ).fetchone()
        return dict(row) if row else None


def count_activities(user_id: int) -> int:
    with get_conn() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM activities WHERE user_id = ?",
            (user_id,),
        ).fetchone()["n"]


# ---------- Streams ----------
# Streams disimpan per activity_id. Karena activity_id unik global (Strava),
# kita gak butuh user_id di tabel streams.
def save_streams(activity_id: int, streams: Dict[str, list]):
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO streams
               (activity_id, time_json, distance_json, heartrate_json,
                velocity_smooth_json, cadence_json, altitude_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (activity_id,
             json.dumps(streams.get("time", [])),
             json.dumps(streams.get("distance", [])),
             json.dumps(streams.get("heartrate", [])),
             json.dumps(streams.get("velocity_smooth", [])),
             json.dumps(streams.get("cadence", [])),
             json.dumps(streams.get("altitude", [])),
             datetime.utcnow().isoformat()),
        )


def get_streams(activity_id: int) -> Optional[Dict[str, list]]:
    with get_conn() as c:
        row = c.execute("SELECT * FROM streams WHERE activity_id = ?", (activity_id,)).fetchone()
        if not row:
            return None
        return {
            "time": json.loads(row["time_json"] or "[]"),
            "distance": json.loads(row["distance_json"] or "[]"),
            "heartrate": json.loads(row["heartrate_json"] or "[]"),
            "velocity_smooth": json.loads(row["velocity_smooth_json"] or "[]"),
            "cadence": json.loads(row["cadence_json"] or "[]"),
            "altitude": json.loads(row["altitude_json"] or "[]"),
        }


def has_streams(activity_id: int) -> bool:
    with get_conn() as c:
        return c.execute(
            "SELECT 1 FROM streams WHERE activity_id = ?", (activity_id,)
        ).fetchone() is not None


# ---------- Training Plan ----------
def save_training_plan(user_id: int, plan_meta: dict, planned_sessions: list) -> None:
    """Persist a training plan plus its day-by-day sessions. One plan per user."""
    with get_conn() as c:
        # Wipe previous plan + its sessions untuk user ini
        c.execute("DELETE FROM planned_sessions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM training_plans WHERE user_id = ?", (user_id,))

        c.execute(
            """INSERT INTO training_plans
               (user_id, race_type, race_distance_km, race_date, target_time_s,
                target_pace_min_per_km, plan_start_date, plan_weeks,
                runs_per_week, peak_weekly_km, current_weekly_km,
                current_ctl, created_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, plan_meta["race_type"], plan_meta["race_distance_km"],
             plan_meta["race_date"], plan_meta["target_time_s"],
             plan_meta.get("target_pace_min_per_km"),
             plan_meta["plan_start_date"], plan_meta["plan_weeks"],
             plan_meta["runs_per_week"], plan_meta.get("peak_weekly_km"),
             plan_meta.get("current_weekly_km"), plan_meta.get("current_ctl"),
             datetime.utcnow().isoformat(), plan_meta.get("notes")),
        )

        for s in planned_sessions:
            c.execute(
                """INSERT INTO planned_sessions
                   (user_id, date, week_num, phase, session_type,
                    distance_km, duration_min, target_pace_min_per_km,
                    target_hr_zone, description, workout_detail, is_quality)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, s["date"], s["week_num"], s["phase"], s["session_type"],
                 s["distance_km"], s["duration_min"],
                 s.get("target_pace_min_per_km"), s.get("target_hr_zone"),
                 s.get("description"), s.get("workout_detail"),
                 1 if s.get("is_quality") else 0),
            )


def get_training_plan(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM training_plans WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None


def get_planned_sessions_df(user_id: int,
                            date_from: Optional[str] = None,
                            date_to: Optional[str] = None) -> pd.DataFrame:
    with get_conn() as c:
        q = "SELECT * FROM planned_sessions WHERE user_id = ?"
        params: list = [user_id]
        if date_from:
            q += " AND date >= ?"
            params.append(date_from)
        if date_to:
            q += " AND date <= ?"
            params.append(date_to)
        q += " ORDER BY date ASC"
        df = pd.read_sql_query(q, c, params=params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["is_quality"] = df["is_quality"].astype(bool)
    return df


def get_planned_session_on(user_id: int, d) -> Optional[dict]:
    """Return planned session for a specific date (date or ISO str)."""
    iso = d.isoformat() if hasattr(d, "isoformat") else str(d)
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM planned_sessions WHERE user_id = ? AND date = ?",
            (user_id, iso),
        ).fetchone()
        return dict(row) if row else None


def link_planned_to_activity(user_id: int, planned_id: int, activity_id: int) -> None:
    """Mark a planned session as executed by a real activity."""
    with get_conn() as c:
        c.execute(
            "UPDATE planned_sessions SET executed_activity_id = ? WHERE user_id = ? AND id = ?",
            (activity_id, user_id, planned_id),
        )


def delete_training_plan(user_id: int) -> None:
    """Wipe the current plan and all its sessions for this user."""
    with get_conn() as c:
        c.execute("DELETE FROM planned_sessions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM training_plans WHERE user_id = ?", (user_id,))
