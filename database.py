"""
Database layer for RunCoach AI — MULTI-USER + dual-dialect.

Mendukung 2 backend:
  - SQLite lokal (default, untuk dev)         → file: data/runcoach.db
  - Postgres (Supabase, untuk production)     → set DATABASE_URL secret

Setiap data (tokens, profile, activities, training_plans) di-scope ke
`user_id` = Strava athlete_id. Connect Strava = otomatis login, gak
perlu password.

Pemilihan dialect ditentukan oleh `config.DATABASE_URL`:
  - `sqlite:///path/to/db`        → SQLite
  - `postgresql://user:pass@...`  → Postgres
"""
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, List, Dict, Any

import pandas as pd
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

import config


# ---------------------------------------------------------
# Engine (lazy singleton)
# ---------------------------------------------------------
_engine: Optional[Engine] = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = config.DATABASE_URL
        kwargs: Dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # Postgres: pool kecil cocok untuk Streamlit (banyak rerun)
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 10
            kwargs["pool_recycle"] = 300
        _engine = create_engine(url, **kwargs)
    return _engine


def dialect() -> str:
    """Return 'sqlite' atau 'postgresql'."""
    return get_engine().dialect.name


@contextmanager
def get_conn():
    """Yield SQLAlchemy connection dengan auto-commit di exit."""
    with get_engine().connect() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------
# Schema (dialect-aware DDL)
# ---------------------------------------------------------
def _schema_statements() -> List[str]:
    """Return list of CREATE TABLE/INDEX statements per current dialect."""
    if dialect() == "postgresql":
        return [
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                athlete_json TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS tokens (
                user_id BIGINT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at BIGINT NOT NULL,
                athlete_json TEXT,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS profile (
                user_id BIGINT PRIMARY KEY,
                name TEXT, age INTEGER,
                resting_hr INTEGER, max_hr INTEGER, fthr INTEGER,
                threshold_pace_min_per_km DOUBLE PRECISION,
                weight_kg DOUBLE PRECISION,
                goal_race TEXT, goal_time TEXT, goal_date TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS activities (
                user_id BIGINT NOT NULL,
                id BIGINT NOT NULL,
                name TEXT, sport_type TEXT,
                start_date TEXT, start_date_local TEXT, timezone TEXT,
                distance_m DOUBLE PRECISION,
                moving_time_s BIGINT, elapsed_time_s BIGINT,
                total_elevation_gain_m DOUBLE PRECISION,
                average_speed_mps DOUBLE PRECISION,
                max_speed_mps DOUBLE PRECISION,
                average_heartrate DOUBLE PRECISION,
                max_heartrate DOUBLE PRECISION,
                average_cadence DOUBLE PRECISION,
                suffer_score DOUBLE PRECISION,
                kudos_count INTEGER,
                has_heartrate INTEGER,
                tss DOUBLE PRECISION,
                intensity_factor DOUBLE PRECISION,
                normalized_pace_min_per_km DOUBLE PRECISION,
                average_pace_min_per_km DOUBLE PRECISION,
                estimated_vo2max DOUBLE PRECISION,
                raw_json TEXT,
                PRIMARY KEY (user_id, id)
            )""",
            """CREATE TABLE IF NOT EXISTS streams (
                activity_id BIGINT PRIMARY KEY,
                time_json TEXT, distance_json TEXT, heartrate_json TEXT,
                velocity_smooth_json TEXT, cadence_json TEXT, altitude_json TEXT,
                fetched_at TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_activities_user_date ON activities(user_id, start_date)",
            "CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport_type)",
            """CREATE TABLE IF NOT EXISTS training_plans (
                user_id BIGINT PRIMARY KEY,
                race_type TEXT NOT NULL,
                race_distance_km DOUBLE PRECISION NOT NULL,
                race_date TEXT NOT NULL,
                target_time_s INTEGER NOT NULL,
                target_pace_min_per_km DOUBLE PRECISION,
                plan_start_date TEXT NOT NULL,
                plan_weeks INTEGER NOT NULL,
                runs_per_week INTEGER NOT NULL,
                peak_weekly_km DOUBLE PRECISION,
                current_weekly_km DOUBLE PRECISION,
                current_ctl DOUBLE PRECISION,
                created_at TEXT NOT NULL,
                notes TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS planned_sessions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                date TEXT NOT NULL,
                week_num INTEGER NOT NULL,
                phase TEXT NOT NULL,
                session_type TEXT NOT NULL,
                distance_km DOUBLE PRECISION,
                duration_min DOUBLE PRECISION,
                target_pace_min_per_km DOUBLE PRECISION,
                target_hr_zone TEXT,
                description TEXT,
                workout_detail TEXT,
                is_quality INTEGER,
                executed_activity_id BIGINT,
                UNIQUE(user_id, date)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_planned_sessions_user_date ON planned_sessions(user_id, date)",
        ]
    # SQLite (default)
    return [
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            athlete_json TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS tokens (
            user_id INTEGER PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            athlete_json TEXT,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS profile (
            user_id INTEGER PRIMARY KEY,
            name TEXT, age INTEGER,
            resting_hr INTEGER, max_hr INTEGER, fthr INTEGER,
            threshold_pace_min_per_km REAL, weight_kg REAL,
            goal_race TEXT, goal_time TEXT, goal_date TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS activities (
            user_id INTEGER NOT NULL,
            id INTEGER NOT NULL,
            name TEXT, sport_type TEXT,
            start_date TEXT, start_date_local TEXT, timezone TEXT,
            distance_m REAL,
            moving_time_s INTEGER, elapsed_time_s INTEGER,
            total_elevation_gain_m REAL,
            average_speed_mps REAL, max_speed_mps REAL,
            average_heartrate REAL, max_heartrate REAL,
            average_cadence REAL, suffer_score REAL,
            kudos_count INTEGER, has_heartrate INTEGER,
            tss REAL, intensity_factor REAL,
            normalized_pace_min_per_km REAL,
            average_pace_min_per_km REAL,
            estimated_vo2max REAL,
            raw_json TEXT,
            PRIMARY KEY (user_id, id)
        )""",
        """CREATE TABLE IF NOT EXISTS streams (
            activity_id INTEGER PRIMARY KEY,
            time_json TEXT, distance_json TEXT, heartrate_json TEXT,
            velocity_smooth_json TEXT, cadence_json TEXT, altitude_json TEXT,
            fetched_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_activities_user_date ON activities(user_id, start_date)",
        "CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport_type)",
        """CREATE TABLE IF NOT EXISTS training_plans (
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
        )""",
        """CREATE TABLE IF NOT EXISTS planned_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            week_num INTEGER NOT NULL,
            phase TEXT NOT NULL,
            session_type TEXT NOT NULL,
            distance_km REAL, duration_min REAL,
            target_pace_min_per_km REAL,
            target_hr_zone TEXT, description TEXT,
            workout_detail TEXT, is_quality INTEGER,
            executed_activity_id INTEGER,
            UNIQUE(user_id, date)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_planned_sessions_user_date ON planned_sessions(user_id, date)",
    ]


# ---------------------------------------------------------
# Legacy SQLite migration (only relevant for old local SQLite DB)
# ---------------------------------------------------------
def _migrate_legacy_single_user_db():
    """
    Migrate DB lama (single-user, id=1 everywhere) ke schema multi-user.
    Hanya jalan kalau dialect = sqlite dan DB lama terdeteksi.
    """
    if dialect() != "sqlite":
        return  # Postgres start fresh, no legacy data

    with get_engine().connect() as conn:
        # Cek apakah tabel tokens punya kolom user_id (= sudah multi-user)
        try:
            cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(tokens)").fetchall()]
        except OperationalError:
            cols = []
        if not cols:
            return  # tabel belum ada, init_db akan handle
        if "user_id" in cols:
            return  # sudah multi-user, no migration

        # Old schema detected. Ambil athlete_id dari row lama
        try:
            row = conn.exec_driver_sql("SELECT * FROM tokens WHERE id = 1").fetchone()
        except OperationalError:
            row = None
        if not row:
            # Drop tabel lama yang kosong, biar init bisa bikin schema baru
            for t in ["tokens", "profile", "activities", "training_plans",
                      "planned_sessions", "streams"]:
                try:
                    conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
                except OperationalError:
                    pass
            conn.commit()
            return

        # row is a tuple — find indices via cursor description
        # Easier: re-fetch as dict via column names
        legacy_user_id = None
        # Reconstruct by index: tokens schema lama = (id, access_token, refresh_token,
        #                                              expires_at, athlete_id, athlete_json, updated_at)
        try:
            legacy_user_id = row[4]  # athlete_id
        except IndexError:
            pass
        athlete_json = None
        try:
            athlete_json = row[5]
        except IndexError:
            pass
        if not legacy_user_id and athlete_json:
            try:
                parsed = json.loads(athlete_json)
                legacy_user_id = parsed.get("id")
            except Exception:
                pass
        if not legacy_user_id:
            legacy_user_id = 0  # placeholder, akan di-claim saat OAuth pertama

        # Rename old tables
        for t in ["tokens", "profile", "activities", "training_plans", "planned_sessions"]:
            try:
                conn.exec_driver_sql(f"ALTER TABLE {t} RENAME TO {t}_legacy")
            except OperationalError:
                pass
        conn.commit()

        # Create new schema
        for stmt in _schema_statements():
            conn.exec_driver_sql(stmt)

        now = datetime.utcnow().isoformat()

        # users
        conn.exec_driver_sql(
            "INSERT OR REPLACE INTO users (user_id, athlete_json, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?)",
            (legacy_user_id, athlete_json, now, now),
        )

        # tokens
        # row: (id, access_token, refresh_token, expires_at, athlete_id, athlete_json, updated_at)
        conn.exec_driver_sql(
            "INSERT INTO tokens (user_id, access_token, refresh_token, expires_at, athlete_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (legacy_user_id, row[1], row[2], row[3], athlete_json, row[6]),
        )

        # profile (lama: id=1, semua kolom profile)
        try:
            prow = conn.exec_driver_sql("SELECT * FROM profile_legacy WHERE id = 1").fetchone()
        except OperationalError:
            prow = None
        if prow:
            # prow: (id, name, age, resting_hr, max_hr, fthr, threshold_pace, weight, goal_race, goal_time, goal_date)
            conn.exec_driver_sql(
                "INSERT INTO profile (user_id, name, age, resting_hr, max_hr, fthr, "
                "threshold_pace_min_per_km, weight_kg, goal_race, goal_time, goal_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (legacy_user_id, prow[1], prow[2], prow[3], prow[4], prow[5],
                 prow[6], prow[7], prow[8], prow[9], prow[10]),
            )

        # activities — copy all rows with user_id prepended
        try:
            cols_old = [r[1] for r in conn.exec_driver_sql(
                "PRAGMA table_info(activities_legacy)").fetchall()]
            arows = conn.exec_driver_sql("SELECT * FROM activities_legacy").fetchall()
        except OperationalError:
            cols_old, arows = [], []
        for a in arows:
            placeholders = ", ".join(["?"] * (len(cols_old) + 1))
            col_list = "user_id, " + ", ".join(cols_old)
            conn.exec_driver_sql(
                f"INSERT OR REPLACE INTO activities ({col_list}) VALUES ({placeholders})",
                tuple([legacy_user_id] + list(a)),
            )

        # training_plans
        try:
            tp = conn.exec_driver_sql(
                "SELECT * FROM training_plans_legacy WHERE id = 1").fetchone()
        except OperationalError:
            tp = None
        if tp:
            # tp: (id, race_type, race_distance_km, race_date, target_time_s,
            #      target_pace, plan_start_date, plan_weeks, runs_per_week,
            #      peak_weekly_km, current_weekly_km, current_ctl, created_at, notes)
            conn.exec_driver_sql(
                "INSERT INTO training_plans (user_id, race_type, race_distance_km, race_date, "
                "target_time_s, target_pace_min_per_km, plan_start_date, plan_weeks, "
                "runs_per_week, peak_weekly_km, current_weekly_km, current_ctl, "
                "created_at, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (legacy_user_id, tp[1], tp[2], tp[3], tp[4], tp[5], tp[6], tp[7],
                 tp[8], tp[9], tp[10], tp[11], tp[12], tp[13]),
            )

        # planned_sessions
        try:
            ps_rows = conn.exec_driver_sql("SELECT * FROM planned_sessions_legacy").fetchall()
        except OperationalError:
            ps_rows = []
        for s in ps_rows:
            # legacy: (id, plan_id, date, week_num, phase, session_type, distance_km,
            #          duration_min, target_pace, target_hr_zone, description,
            #          workout_detail, is_quality, executed_activity_id)
            conn.exec_driver_sql(
                "INSERT OR REPLACE INTO planned_sessions "
                "(user_id, date, week_num, phase, session_type, distance_km, duration_min, "
                "target_pace_min_per_km, target_hr_zone, description, workout_detail, "
                "is_quality, executed_activity_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (legacy_user_id, s[2], s[3], s[4], s[5], s[6], s[7],
                 s[8], s[9], s[10], s[11], s[12], s[13]),
            )

        # Drop legacy tables
        for t in ["tokens_legacy", "profile_legacy", "activities_legacy",
                  "training_plans_legacy", "planned_sessions_legacy"]:
            try:
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
            except OperationalError:
                pass

        conn.commit()


def init_db():
    """Bootstrap database — run migration kalau perlu, lalu create schema baru."""
    _migrate_legacy_single_user_db()
    with get_conn() as c:
        for stmt in _schema_statements():
            c.execute(text(stmt))


# ---------------------------------------------------------
# UPSERT helper — modern SQLite (3.24+) dan Postgres sama syntax-nya
# ---------------------------------------------------------
def _bool_int(v) -> int:
    return 1 if v else 0


# ---------------------------------------------------------
# Users
# ---------------------------------------------------------
def upsert_user(user_id: int, athlete: Optional[dict] = None):
    """Catat user lewat OAuth. Idempotent."""
    now = datetime.utcnow().isoformat()
    aj = json.dumps(athlete) if athlete else None
    with get_conn() as c:
        existing = c.execute(
            text("SELECT user_id FROM users WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        if existing:
            c.execute(
                text(
                    "UPDATE users SET "
                    "athlete_json = COALESCE(:aj, athlete_json), "
                    "last_seen = :now "
                    "WHERE user_id = :uid"
                ),
                {"aj": aj, "now": now, "uid": user_id},
            )
        else:
            c.execute(
                text(
                    "INSERT INTO users (user_id, athlete_json, first_seen, last_seen) "
                    "VALUES (:uid, :aj, :now, :now)"
                ),
                {"uid": user_id, "aj": aj, "now": now},
            )


def get_user(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            text("SELECT * FROM users WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        return dict(row._mapping) if row else None


def list_users() -> List[dict]:
    with get_conn() as c:
        rows = c.execute(text(
            "SELECT * FROM users WHERE user_id > 0 ORDER BY last_seen DESC"
        )).fetchall()
    return [dict(r._mapping) for r in rows]


def has_orphan_data() -> bool:
    """Cek apakah ada data placeholder dari legacy migration (user_id = 0)."""
    with get_conn() as c:
        try:
            n = c.execute(text("SELECT COUNT(*) FROM activities WHERE user_id = 0")).scalar()
            if n and n > 0:
                return True
            n = c.execute(text("SELECT COUNT(*) FROM training_plans WHERE user_id = 0")).scalar()
            return bool(n and n > 0)
        except (OperationalError, ProgrammingError):
            return False


def claim_orphan_data(new_user_id: int) -> dict:
    """Pindahkan data dengan user_id=0 ke `new_user_id`. Return rowcount per tabel."""
    moved = {}
    with get_conn() as c:
        for table in ["activities", "training_plans", "planned_sessions", "profile"]:
            try:
                if table in ("profile", "training_plans"):
                    c.execute(
                        text(f"DELETE FROM {table} WHERE user_id = :uid"),
                        {"uid": new_user_id},
                    )
                res = c.execute(
                    text(f"UPDATE {table} SET user_id = :uid WHERE user_id = 0"),
                    {"uid": new_user_id},
                )
                moved[table] = res.rowcount
            except (OperationalError, ProgrammingError) as e:
                moved[table] = f"error: {e}"
        try:
            c.execute(text("DELETE FROM tokens WHERE user_id = 0"))
        except (OperationalError, ProgrammingError):
            pass
        try:
            c.execute(text("DELETE FROM users WHERE user_id = 0"))
        except (OperationalError, ProgrammingError):
            pass
    return moved


# ---------------------------------------------------------
# Tokens
# ---------------------------------------------------------
def save_tokens(user_id: int, access: str, refresh: str, expires_at: int,
                athlete: Optional[dict] = None):
    aj = json.dumps(athlete) if athlete else None
    with get_conn() as c:
        c.execute(
            text(
                "INSERT INTO tokens (user_id, access_token, refresh_token, expires_at, "
                "athlete_json, updated_at) "
                "VALUES (:uid, :at, :rt, :exp, :aj, :upd) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "access_token = excluded.access_token, "
                "refresh_token = excluded.refresh_token, "
                "expires_at = excluded.expires_at, "
                "athlete_json = COALESCE(excluded.athlete_json, tokens.athlete_json), "
                "updated_at = excluded.updated_at"
            ),
            {"uid": user_id, "at": access, "rt": refresh, "exp": expires_at,
             "aj": aj, "upd": datetime.utcnow().isoformat()},
        )


def get_tokens(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            text("SELECT * FROM tokens WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        return dict(row._mapping) if row else None


def clear_tokens(user_id: int):
    with get_conn() as c:
        c.execute(text("DELETE FROM tokens WHERE user_id = :uid"), {"uid": user_id})


# ---------------------------------------------------------
# Profile
# ---------------------------------------------------------
def save_profile(user_id: int, p: dict):
    with get_conn() as c:
        c.execute(
            text(
                "INSERT INTO profile (user_id, name, age, resting_hr, max_hr, fthr, "
                "threshold_pace_min_per_km, weight_kg, goal_race, goal_time, goal_date) "
                "VALUES (:uid, :nm, :age, :rhr, :mhr, :fthr, :tp, :wt, :gr, :gt, :gd) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "name = excluded.name, age = excluded.age, "
                "resting_hr = excluded.resting_hr, max_hr = excluded.max_hr, "
                "fthr = excluded.fthr, "
                "threshold_pace_min_per_km = excluded.threshold_pace_min_per_km, "
                "weight_kg = excluded.weight_kg, goal_race = excluded.goal_race, "
                "goal_time = excluded.goal_time, goal_date = excluded.goal_date"
            ),
            {
                "uid": user_id, "nm": p.get("name"), "age": p.get("age"),
                "rhr": p.get("resting_hr"), "mhr": p.get("max_hr"),
                "fthr": p.get("fthr"),
                "tp": p.get("threshold_pace_min_per_km"),
                "wt": p.get("weight_kg"), "gr": p.get("goal_race"),
                "gt": p.get("goal_time"), "gd": p.get("goal_date"),
            },
        )


def get_profile(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            text("SELECT * FROM profile WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        return dict(row._mapping) if row else None


# ---------------------------------------------------------
# Activities
# ---------------------------------------------------------
def upsert_activity(user_id: int, act: Dict[str, Any]):
    with get_conn() as c:
        c.execute(
            text(
                "INSERT INTO activities (user_id, id, name, sport_type, start_date, "
                "start_date_local, timezone, distance_m, moving_time_s, elapsed_time_s, "
                "total_elevation_gain_m, average_speed_mps, max_speed_mps, "
                "average_heartrate, max_heartrate, average_cadence, suffer_score, "
                "kudos_count, has_heartrate, raw_json) "
                "VALUES (:uid, :id, :nm, :st, :sd, :sdl, :tz, :dm, :mt, :et, :elev, "
                ":avs, :mxs, :avhr, :mxhr, :avc, :ss, :kc, :hhr, :rj) "
                "ON CONFLICT (user_id, id) DO UPDATE SET "
                "name = excluded.name, sport_type = excluded.sport_type, "
                "start_date = excluded.start_date, start_date_local = excluded.start_date_local, "
                "timezone = excluded.timezone, distance_m = excluded.distance_m, "
                "moving_time_s = excluded.moving_time_s, elapsed_time_s = excluded.elapsed_time_s, "
                "total_elevation_gain_m = excluded.total_elevation_gain_m, "
                "average_speed_mps = excluded.average_speed_mps, "
                "max_speed_mps = excluded.max_speed_mps, "
                "average_heartrate = excluded.average_heartrate, "
                "max_heartrate = excluded.max_heartrate, "
                "average_cadence = excluded.average_cadence, "
                "suffer_score = excluded.suffer_score, "
                "kudos_count = excluded.kudos_count, "
                "has_heartrate = excluded.has_heartrate, "
                "raw_json = excluded.raw_json"
            ),
            {
                "uid": user_id, "id": act.get("id"), "nm": act.get("name"),
                "st": act.get("sport_type") or act.get("type"),
                "sd": act.get("start_date"), "sdl": act.get("start_date_local"),
                "tz": act.get("timezone"), "dm": act.get("distance"),
                "mt": act.get("moving_time"), "et": act.get("elapsed_time"),
                "elev": act.get("total_elevation_gain"),
                "avs": act.get("average_speed"), "mxs": act.get("max_speed"),
                "avhr": act.get("average_heartrate"),
                "mxhr": act.get("max_heartrate"),
                "avc": act.get("average_cadence"),
                "ss": act.get("suffer_score"),
                "kc": act.get("kudos_count"),
                "hhr": _bool_int(act.get("has_heartrate")),
                "rj": json.dumps(act),
            },
        )


def update_activity_metrics(user_id: int, activity_id: int, metrics: Dict[str, Any]):
    with get_conn() as c:
        c.execute(
            text(
                "UPDATE activities SET "
                "tss = :tss, intensity_factor = :if_, "
                "normalized_pace_min_per_km = :np, "
                "average_pace_min_per_km = :ap, "
                "estimated_vo2max = :vo2 "
                "WHERE user_id = :uid AND id = :aid"
            ),
            {
                "tss": metrics.get("tss"), "if_": metrics.get("intensity_factor"),
                "np": metrics.get("normalized_pace_min_per_km"),
                "ap": metrics.get("average_pace_min_per_km"),
                "vo2": metrics.get("estimated_vo2max"),
                "uid": user_id, "aid": activity_id,
            },
        )


def get_latest_activity_date(user_id: int) -> Optional[str]:
    with get_conn() as c:
        row = c.execute(
            text("SELECT MAX(start_date) AS last FROM activities WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        return row[0] if row and row[0] else None


def get_activities_df(user_id: int, sport_filter: Optional[str] = "Run") -> pd.DataFrame:
    q = "SELECT * FROM activities WHERE user_id = :uid"
    params: Dict[str, Any] = {"uid": user_id}
    if sport_filter:
        q += " AND sport_type LIKE :sf"
        params["sf"] = f"%{sport_filter}%"
    q += " ORDER BY start_date DESC"
    with get_engine().connect() as conn:
        df = pd.read_sql_query(text(q), conn, params=params)
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
            text("SELECT * FROM activities WHERE user_id = :uid AND id = :aid"),
            {"uid": user_id, "aid": activity_id},
        ).fetchone()
        return dict(row._mapping) if row else None


def count_activities(user_id: int) -> int:
    with get_conn() as c:
        n = c.execute(
            text("SELECT COUNT(*) FROM activities WHERE user_id = :uid"),
            {"uid": user_id},
        ).scalar()
    return int(n or 0)


# ---------------------------------------------------------
# Streams (per activity_id, no user_id needed)
# ---------------------------------------------------------
def save_streams(activity_id: int, streams: Dict[str, list]):
    with get_conn() as c:
        c.execute(
            text(
                "INSERT INTO streams (activity_id, time_json, distance_json, "
                "heartrate_json, velocity_smooth_json, cadence_json, altitude_json, "
                "fetched_at) "
                "VALUES (:aid, :t, :d, :h, :v, :c, :al, :f) "
                "ON CONFLICT (activity_id) DO UPDATE SET "
                "time_json = excluded.time_json, "
                "distance_json = excluded.distance_json, "
                "heartrate_json = excluded.heartrate_json, "
                "velocity_smooth_json = excluded.velocity_smooth_json, "
                "cadence_json = excluded.cadence_json, "
                "altitude_json = excluded.altitude_json, "
                "fetched_at = excluded.fetched_at"
            ),
            {
                "aid": activity_id,
                "t": json.dumps(streams.get("time", [])),
                "d": json.dumps(streams.get("distance", [])),
                "h": json.dumps(streams.get("heartrate", [])),
                "v": json.dumps(streams.get("velocity_smooth", [])),
                "c": json.dumps(streams.get("cadence", [])),
                "al": json.dumps(streams.get("altitude", [])),
                "f": datetime.utcnow().isoformat(),
            },
        )


def get_streams(activity_id: int) -> Optional[Dict[str, list]]:
    with get_conn() as c:
        row = c.execute(
            text("SELECT * FROM streams WHERE activity_id = :aid"),
            {"aid": activity_id},
        ).fetchone()
        if not row:
            return None
        m = row._mapping
        return {
            "time": json.loads(m["time_json"] or "[]"),
            "distance": json.loads(m["distance_json"] or "[]"),
            "heartrate": json.loads(m["heartrate_json"] or "[]"),
            "velocity_smooth": json.loads(m["velocity_smooth_json"] or "[]"),
            "cadence": json.loads(m["cadence_json"] or "[]"),
            "altitude": json.loads(m["altitude_json"] or "[]"),
        }


def has_streams(activity_id: int) -> bool:
    with get_conn() as c:
        n = c.execute(
            text("SELECT 1 FROM streams WHERE activity_id = :aid"),
            {"aid": activity_id},
        ).fetchone()
    return n is not None


# ---------------------------------------------------------
# Training Plan
# ---------------------------------------------------------
def save_training_plan(user_id: int, plan_meta: dict, planned_sessions: list) -> None:
    with get_conn() as c:
        # Wipe previous plan + sessions untuk user ini
        c.execute(text("DELETE FROM planned_sessions WHERE user_id = :uid"),
                  {"uid": user_id})
        c.execute(text("DELETE FROM training_plans WHERE user_id = :uid"),
                  {"uid": user_id})

        c.execute(
            text(
                "INSERT INTO training_plans (user_id, race_type, race_distance_km, "
                "race_date, target_time_s, target_pace_min_per_km, plan_start_date, "
                "plan_weeks, runs_per_week, peak_weekly_km, current_weekly_km, "
                "current_ctl, created_at, notes) "
                "VALUES (:uid, :rt, :rd, :rdt, :tt, :tp, :psd, :pw, :rpw, :pkw, "
                ":cwk, :cctl, :ca, :nt)"
            ),
            {
                "uid": user_id, "rt": plan_meta["race_type"],
                "rd": plan_meta["race_distance_km"],
                "rdt": plan_meta["race_date"], "tt": plan_meta["target_time_s"],
                "tp": plan_meta.get("target_pace_min_per_km"),
                "psd": plan_meta["plan_start_date"],
                "pw": plan_meta["plan_weeks"],
                "rpw": plan_meta["runs_per_week"],
                "pkw": plan_meta.get("peak_weekly_km"),
                "cwk": plan_meta.get("current_weekly_km"),
                "cctl": plan_meta.get("current_ctl"),
                "ca": datetime.utcnow().isoformat(),
                "nt": plan_meta.get("notes"),
            },
        )

        for s in planned_sessions:
            c.execute(
                text(
                    "INSERT INTO planned_sessions (user_id, date, week_num, phase, "
                    "session_type, distance_km, duration_min, target_pace_min_per_km, "
                    "target_hr_zone, description, workout_detail, is_quality) "
                    "VALUES (:uid, :dt, :wn, :ph, :st, :dk, :dm, :tp, :thz, :ds, "
                    ":wd, :iq)"
                ),
                {
                    "uid": user_id, "dt": s["date"], "wn": s["week_num"],
                    "ph": s["phase"], "st": s["session_type"],
                    "dk": s["distance_km"], "dm": s["duration_min"],
                    "tp": s.get("target_pace_min_per_km"),
                    "thz": s.get("target_hr_zone"),
                    "ds": s.get("description"),
                    "wd": s.get("workout_detail"),
                    "iq": _bool_int(s.get("is_quality")),
                },
            )


def get_training_plan(user_id: int) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            text("SELECT * FROM training_plans WHERE user_id = :uid"),
            {"uid": user_id},
        ).fetchone()
        return dict(row._mapping) if row else None


def get_planned_sessions_df(user_id: int,
                            date_from: Optional[str] = None,
                            date_to: Optional[str] = None) -> pd.DataFrame:
    q = "SELECT * FROM planned_sessions WHERE user_id = :uid"
    params: Dict[str, Any] = {"uid": user_id}
    if date_from:
        q += " AND date >= :df"
        params["df"] = date_from
    if date_to:
        q += " AND date <= :dt"
        params["dt"] = date_to
    q += " ORDER BY date ASC"
    with get_engine().connect() as conn:
        df = pd.read_sql_query(text(q), conn, params=params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["is_quality"] = df["is_quality"].astype(bool)
    return df


def get_planned_session_on(user_id: int, d) -> Optional[dict]:
    iso = d.isoformat() if hasattr(d, "isoformat") else str(d)
    with get_conn() as c:
        row = c.execute(
            text("SELECT * FROM planned_sessions WHERE user_id = :uid AND date = :dt"),
            {"uid": user_id, "dt": iso},
        ).fetchone()
        return dict(row._mapping) if row else None


def link_planned_to_activity(user_id: int, planned_id: int, activity_id: int) -> None:
    with get_conn() as c:
        c.execute(
            text(
                "UPDATE planned_sessions SET executed_activity_id = :aid "
                "WHERE user_id = :uid AND id = :pid"
            ),
            {"aid": activity_id, "uid": user_id, "pid": planned_id},
        )


def delete_training_plan(user_id: int) -> None:
    with get_conn() as c:
        c.execute(text("DELETE FROM planned_sessions WHERE user_id = :uid"),
                  {"uid": user_id})
        c.execute(text("DELETE FROM training_plans WHERE user_id = :uid"),
                  {"uid": user_id})
