#!/usr/bin/env python3
"""
Migrasi data dari local SQLite ke Postgres (Supabase).

Usage (dari root project):
    export DATABASE_URL="postgresql://postgres.xxxxx:pwd@aws-1-ap-southeast-1.pooler.supabase.com:6543/postgres"
    python3 scripts/migrate_to_postgres.py

Atau kasih URL sebagai argumen:
    python3 scripts/migrate_to_postgres.py "postgresql://..."

Script ini:
1. Buka local SQLite di data/runcoach.db
2. Auto-detect schema lama (single-user, id=1) dan migrate ke multi-user
3. Copy semua data ke Postgres:
   - users, tokens, profile, activities, streams, training_plans, planned_sessions
4. Print summary

Bisa di-rerun aman (pakai UPSERT) — kalau ada conflict, row di-update.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

LOCAL_DB = Path(os.getenv("SOURCE_DB", ROOT / "data" / "runcoach.db"))


def fail(msg: str, code: int = 1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------
if not LOCAL_DB.exists():
    fail(f"Local SQLite tidak ditemukan di {LOCAL_DB}")

dest_url = os.getenv("DATABASE_URL")
if not dest_url and len(sys.argv) > 1:
    dest_url = sys.argv[1]
if not dest_url:
    fail("Set DATABASE_URL env var atau kasih URL sebagai argumen pertama.\n"
         "Contoh: python3 scripts/migrate_to_postgres.py 'postgresql://...'")
if not dest_url.startswith(("postgresql://", "postgres://")):
    fail(f"DATABASE_URL harus Postgres URL, dapat: {dest_url[:30]}...")

# Normalisasi: "postgres://" -> "postgresql://" (SQLAlchemy prefers latter)
if dest_url.startswith("postgres://"):
    dest_url = "postgresql://" + dest_url[len("postgres://"):]

print(f"📂 Source : {LOCAL_DB}")
print(f"📡 Dest   : {dest_url[:60]}...")
print()

# ---------------------------------------------------------
# Step 0: Auto-backup local SQLite (jaga-jaga kalau ada apa-apa)
# ---------------------------------------------------------
import shutil
from datetime import datetime
backup_path = LOCAL_DB.with_suffix(f".pre_migrate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
shutil.copy2(LOCAL_DB, backup_path)
print(f"💾 Backup local SQLite → {backup_path.name}")
print()

# ---------------------------------------------------------
# Step 1: ensure local SQLite is on multi-user schema
# ---------------------------------------------------------
print("📦 Step 1: Pastikan local SQLite schema multi-user...")
config.DATABASE_URL = f"sqlite:///{LOCAL_DB}"
import database as db  # import setelah override DATABASE_URL
db.init_db()
print(f"   ✅ Local SQLite ready (dialect: {db.dialect()})")

# Read source via raw sqlite3 (avoid SQLAlchemy singleton confusion)
src = sqlite3.connect(LOCAL_DB)
src.row_factory = sqlite3.Row


def fetch_all(table: str) -> list:
    try:
        return [dict(r) for r in src.execute(f"SELECT * FROM {table}").fetchall()]
    except sqlite3.OperationalError:
        return []


users_rows = fetch_all("users")
tokens_rows = fetch_all("tokens")
profile_rows = fetch_all("profile")
activities_rows = fetch_all("activities")
streams_rows = fetch_all("streams")
plans_rows = fetch_all("training_plans")
sessions_rows = fetch_all("planned_sessions")
src.close()

print(f"   📊 Read dari local:")
print(f"      users           : {len(users_rows)}")
print(f"      tokens          : {len(tokens_rows)}")
print(f"      profile         : {len(profile_rows)}")
print(f"      activities      : {len(activities_rows)}")
print(f"      streams         : {len(streams_rows)}")
print(f"      training_plans  : {len(plans_rows)}")
print(f"      planned_sessions: {len(sessions_rows)}")
print()

# ---------------------------------------------------------
# Step 2: Connect to Postgres + init schema
# ---------------------------------------------------------
print("📡 Step 2: Connect Postgres + init schema...")

# Reset module-level engine biar pakai DATABASE_URL baru
config.DATABASE_URL = dest_url
db._engine = None  # reset singleton
db.init_db()
print(f"   ✅ Postgres ready (dialect: {db.dialect()})")
print()

# ---------------------------------------------------------
# Step 3: Insert ke Postgres
# ---------------------------------------------------------
print("📤 Step 3: Copy data ke Postgres...")
from sqlalchemy import text


def insert_users():
    if not users_rows:
        return 0
    with db.get_conn() as c:
        n = 0
        for r in users_rows:
            c.execute(
                text(
                    "INSERT INTO users (user_id, athlete_json, first_seen, last_seen) "
                    "VALUES (:uid, :aj, :fs, :ls) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "athlete_json = COALESCE(excluded.athlete_json, users.athlete_json), "
                    "last_seen = excluded.last_seen"
                ),
                {"uid": r["user_id"], "aj": r.get("athlete_json"),
                 "fs": r["first_seen"], "ls": r["last_seen"]},
            )
            n += 1
    return n


def insert_tokens():
    n = 0
    for r in tokens_rows:
        db.save_tokens(
            r["user_id"], r["access_token"], r["refresh_token"],
            r["expires_at"],
            json.loads(r["athlete_json"]) if r.get("athlete_json") else None,
        )
        n += 1
    return n


def insert_profile():
    n = 0
    for r in profile_rows:
        p = {k: r.get(k) for k in
             ("name", "age", "resting_hr", "max_hr", "fthr",
              "threshold_pace_min_per_km", "weight_kg",
              "goal_race", "goal_time", "goal_date")}
        db.save_profile(r["user_id"], p)
        n += 1
    return n


def insert_activities():
    if not activities_rows:
        return 0
    n = 0
    with db.get_conn() as c:
        for r in activities_rows:
            c.execute(
                text(
                    "INSERT INTO activities (user_id, id, name, sport_type, start_date, "
                    "start_date_local, timezone, distance_m, moving_time_s, elapsed_time_s, "
                    "total_elevation_gain_m, average_speed_mps, max_speed_mps, "
                    "average_heartrate, max_heartrate, average_cadence, suffer_score, "
                    "kudos_count, has_heartrate, tss, intensity_factor, "
                    "normalized_pace_min_per_km, average_pace_min_per_km, "
                    "estimated_vo2max, raw_json) "
                    "VALUES (:uid, :id, :nm, :st, :sd, :sdl, :tz, :dm, :mt, :et, :elev, "
                    ":avs, :mxs, :avhr, :mxhr, :avc, :ss, :kc, :hhr, :tss, :if_, "
                    ":np, :ap, :vo2, :rj) "
                    "ON CONFLICT (user_id, id) DO NOTHING"
                ),
                {
                    "uid": r["user_id"], "id": r["id"], "nm": r.get("name"),
                    "st": r.get("sport_type"),
                    "sd": r.get("start_date"), "sdl": r.get("start_date_local"),
                    "tz": r.get("timezone"), "dm": r.get("distance_m"),
                    "mt": r.get("moving_time_s"), "et": r.get("elapsed_time_s"),
                    "elev": r.get("total_elevation_gain_m"),
                    "avs": r.get("average_speed_mps"),
                    "mxs": r.get("max_speed_mps"),
                    "avhr": r.get("average_heartrate"),
                    "mxhr": r.get("max_heartrate"),
                    "avc": r.get("average_cadence"),
                    "ss": r.get("suffer_score"),
                    "kc": r.get("kudos_count"),
                    "hhr": r.get("has_heartrate"),
                    "tss": r.get("tss"),
                    "if_": r.get("intensity_factor"),
                    "np": r.get("normalized_pace_min_per_km"),
                    "ap": r.get("average_pace_min_per_km"),
                    "vo2": r.get("estimated_vo2max"),
                    "rj": r.get("raw_json"),
                },
            )
            n += 1
            if n % 100 == 0:
                print(f"      ... {n} activities", flush=True)
    return n


def insert_streams():
    if not streams_rows:
        return 0
    n = 0
    with db.get_conn() as c:
        for r in streams_rows:
            c.execute(
                text(
                    "INSERT INTO streams (activity_id, time_json, distance_json, "
                    "heartrate_json, velocity_smooth_json, cadence_json, altitude_json, "
                    "fetched_at) "
                    "VALUES (:aid, :t, :d, :h, :v, :c, :al, :f) "
                    "ON CONFLICT (activity_id) DO NOTHING"
                ),
                {
                    "aid": r["activity_id"],
                    "t": r.get("time_json"), "d": r.get("distance_json"),
                    "h": r.get("heartrate_json"),
                    "v": r.get("velocity_smooth_json"),
                    "c": r.get("cadence_json"),
                    "al": r.get("altitude_json"),
                    "f": r.get("fetched_at"),
                },
            )
            n += 1
            if n % 100 == 0:
                print(f"      ... {n} streams", flush=True)
    return n


def insert_plans_and_sessions():
    n_plan, n_sess = 0, 0
    for r in plans_rows:
        plan_meta = {
            "race_type": r["race_type"],
            "race_distance_km": r["race_distance_km"],
            "race_date": r["race_date"],
            "target_time_s": r["target_time_s"],
            "target_pace_min_per_km": r.get("target_pace_min_per_km"),
            "plan_start_date": r["plan_start_date"],
            "plan_weeks": r["plan_weeks"],
            "runs_per_week": r["runs_per_week"],
            "peak_weekly_km": r.get("peak_weekly_km"),
            "current_weekly_km": r.get("current_weekly_km"),
            "current_ctl": r.get("current_ctl"),
            "notes": r.get("notes"),
        }
        # ambil planned sessions untuk user ini
        user_sessions = [
            {
                "date": s["date"], "week_num": s["week_num"],
                "phase": s["phase"], "session_type": s["session_type"],
                "distance_km": s.get("distance_km"),
                "duration_min": s.get("duration_min"),
                "target_pace_min_per_km": s.get("target_pace_min_per_km"),
                "target_hr_zone": s.get("target_hr_zone"),
                "description": s.get("description"),
                "workout_detail": s.get("workout_detail"),
                "is_quality": s.get("is_quality"),
            }
            for s in sessions_rows if s["user_id"] == r["user_id"]
        ]
        db.save_training_plan(r["user_id"], plan_meta, user_sessions)
        n_plan += 1
        n_sess += len(user_sessions)
    return n_plan, n_sess


n_u = insert_users()
print(f"   ✅ users           : {n_u}")
n_t = insert_tokens()
print(f"   ✅ tokens          : {n_t}")
n_p = insert_profile()
print(f"   ✅ profile         : {n_p}")
n_a = insert_activities()
print(f"   ✅ activities      : {n_a}")
n_s = insert_streams()
print(f"   ✅ streams         : {n_s}")
n_pl, n_ps = insert_plans_and_sessions()
print(f"   ✅ training_plans  : {n_pl}")
print(f"   ✅ planned_sessions: {n_ps}")
print()
print("🎉 Migration selesai!")
print()
print("Next steps:")
print("  1. Tambah DATABASE_URL ke Streamlit Cloud secrets")
print("     (App settings → Secrets, tambah baris baru):")
print(f"     DATABASE_URL = \"{dest_url}\"")
print("  2. Streamlit Cloud akan auto-reboot dan pakai Postgres.")
print("  3. Test app — semua data harus muncul.")
