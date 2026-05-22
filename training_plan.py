"""
Adaptive training plan generator for RunCoach AI.

Supports multiple race distances: 5K, 10K, Half Marathon, Marathon, Ultra (50K).
Generates structured periodized plans, then dynamically adjusts daily sessions
based on form (TSB), missed sessions, and athlete state.

Periodization model: Base → Build → Peak → Taper
References:
- Jack Daniels, "Daniels' Running Formula" (T-pace, I-pace, R-pace)
- Pete Pfitzinger, "Advanced Marathoning"
- Joe Friel, "The Triathlete's Training Bible" (periodization)
- Pete Magill / Hal Higdon (5K/10K plans)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import analytics as A


# ============================================================
# Session types & emoji
# ============================================================
SESSION_TYPES = {
    "Rest":      "🛌",
    "Easy":      "🟢",
    "Recovery":  "🟢",
    "Long":      "🔵",
    "Tempo":     "🟡",
    "Threshold": "🟠",
    "MP":        "🟣",   # Marathon pace
    "HMP":       "🟣",   # Half marathon pace
    "VO2":       "🔴",
    "Intervals": "🔴",
    "Strides":   "⚡",
    "Race":      "🏁",
}


# ============================================================
# Race profiles
# ============================================================
# For each race we define:
#   distance_km        — official race distance
#   default_weeks      — typical plan length
#   peak_long_km       — longest single training run
#   default_peak_km    — typical peak weekly volume for an intermediate runner
#   primary_quality    — main quality session type used in Build/Peak
#   suggested_paces    — multiplier (vs marathon-pace ratio of effort) for race pace
RACE_PROFILES: Dict[str, dict] = {
    "5K": {
        "distance_km": 5.0,
        "default_weeks": 8,
        "peak_long_km": 16.0,
        "default_peak_km": 50.0,
        "build_quality": "VO2",
        "peak_quality": "VO2",
        "tagline": "Fast & punchy. Lots of VO2 max work + short intervals.",
    },
    "10K": {
        "distance_km": 10.0,
        "default_weeks": 10,
        "peak_long_km": 20.0,
        "default_peak_km": 60.0,
        "build_quality": "Threshold",
        "peak_quality": "VO2",
        "tagline": "Balance of threshold + VO2 max. Pain tolerance is key.",
    },
    "Half Marathon": {
        "distance_km": 21.0975,
        "default_weeks": 12,
        "peak_long_km": 24.0,
        "default_peak_km": 65.0,
        "build_quality": "Threshold",
        "peak_quality": "Threshold",
        "tagline": "Threshold + steady-state work. Endurance with speed.",
    },
    "Marathon": {
        "distance_km": 42.195,
        "default_weeks": 16,
        "peak_long_km": 32.0,
        "default_peak_km": 75.0,
        "build_quality": "Threshold",
        "peak_quality": "MP",
        "tagline": "Endurance king. Long runs + marathon-pace work.",
    },
    "Ultra (50K)": {
        "distance_km": 50.0,
        "default_weeks": 20,
        "peak_long_km": 38.0,
        "default_peak_km": 85.0,
        "build_quality": "Threshold",
        "peak_quality": "Long",   # extra long runs dominate
        "tagline": "Volume + time on feet. Back-to-back long runs.",
    },
}


def race_options() -> List[str]:
    """For dropdowns."""
    return list(RACE_PROFILES.keys())


def race_default_target_seconds(race_type: str) -> int:
    """Sensible default goal time per race (intermediate runner)."""
    defaults = {
        "5K": 25 * 60,             # 25:00
        "10K": 55 * 60,            # 55:00
        "Half Marathon": 2 * 3600,  # 2:00:00
        "Marathon": 4 * 3600,       # 4:00:00
        "Ultra (50K)": 6 * 3600,    # 6:00:00 (very rough)
    }
    return defaults.get(race_type, 3600)


# ============================================================
# Plan dataclass
# ============================================================
@dataclass
class PlannedSession:
    """One day in the plan."""
    date: date
    week_num: int
    phase: str               # "Base" / "Build" / "Peak" / "Taper" / "Race"
    session_type: str        # "Easy", "Long", "Tempo", etc.
    distance_km: float       # Target distance
    duration_min: float      # Target duration (estimate)
    target_pace_min_per_km: Optional[float]
    target_hr_zone: str      # e.g. "Z2", "Z4"
    description: str         # Human-readable session description
    workout_detail: str      # Detailed workout (intervals etc.)
    is_quality: bool         # True if hard/quality session

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d


# ============================================================
# Pace derivation from race target
# ============================================================
def race_paces(race_type: str, target_time_s: int) -> Dict[str, float]:
    """
    Derive training paces (min/km) from goal race pace.

    Using Daniels-style ratios anchored to the race's target pace.
    All offsets in min/km from race-pace.
    """
    profile = RACE_PROFILES[race_type]
    race_pace = (target_time_s / 60.0) / profile["distance_km"]  # min/km

    # Offsets from race pace -> training pace.
    # These approximate Jack Daniels VDOT relationships.
    # For longer races, race pace ≈ closer to threshold;
    # for shorter races, race pace is faster than threshold.
    if race_type == "5K":
        # 5K pace ≈ VO2 pace. So everything else is slower than RP.
        return {
            "RP":        race_pace,                # 5K race pace
            "VO2":       race_pace,                # = 5K pace
            "Threshold": race_pace + 0.25,         # ~10K pace
            "Tempo":     race_pace + 0.50,
            "MP":        race_pace + 0.90,         # marathon pace (rarely used here)
            "HMP":       race_pace + 0.55,
            "Long":      race_pace + 1.20,
            "Easy":      race_pace + 1.45,
            "Recovery":  race_pace + 1.70,
            "Strides":   max(race_pace - 0.40, 2.5),
        }
    if race_type == "10K":
        return {
            "RP":        race_pace,                # 10K race pace
            "VO2":       race_pace - 0.20,         # 5K pace (faster)
            "Threshold": race_pace + 0.05,         # ~threshold
            "Tempo":     race_pace + 0.25,
            "MP":        race_pace + 0.70,
            "HMP":       race_pace + 0.35,
            "Long":      race_pace + 1.00,
            "Easy":      race_pace + 1.25,
            "Recovery":  race_pace + 1.50,
            "Strides":   max(race_pace - 0.60, 2.5),
        }
    if race_type == "Half Marathon":
        return {
            "RP":        race_pace,                # HMP
            "HMP":       race_pace,
            "Threshold": race_pace - 0.10,         # threshold ~10s/km faster than HMP
            "VO2":       race_pace - 0.45,         # ~5K pace
            "Tempo":     race_pace + 0.15,
            "MP":        race_pace + 0.30,
            "Long":      race_pace + 0.85,
            "Easy":      race_pace + 1.10,
            "Recovery":  race_pace + 1.35,
            "Strides":   max(race_pace - 0.80, 2.5),
        }
    if race_type == "Marathon":
        return {
            "RP":        race_pace,                # MP
            "MP":        race_pace,
            "HMP":       race_pace - 0.20,
            "Threshold": race_pace - 0.40,
            "VO2":       race_pace - 0.80,
            "Tempo":     race_pace - 0.20,
            "Long":      race_pace + 0.55,
            "Easy":      race_pace + 0.85,
            "Recovery":  race_pace + 1.10,
            "Strides":   max(race_pace - 1.20, 2.5),
        }
    # Ultra
    return {
        "RP":        race_pace,                # very easy, mostly Z2
        "MP":        race_pace - 0.30,
        "HMP":       race_pace - 0.55,
        "Threshold": race_pace - 0.75,
        "VO2":       race_pace - 1.10,
        "Tempo":     race_pace - 0.55,
        "Long":      race_pace + 0.20,
        "Easy":      race_pace + 0.40,
        "Recovery":  race_pace + 0.70,
        "Strides":   max(race_pace - 1.50, 2.5),
    }


# ============================================================
# Volume / long-run progression curves
# ============================================================
# Each curve is normalized: weeks span 0..1 (start..race). We linearly resample
# to the chosen plan length. Values are % of peak weekly volume and % of peak long run.
VOLUME_CURVE = {
    "5K": [
        # Base
        0.55, 0.65, 0.75, 0.55,
        # Build/Peak
        0.85, 0.95, 1.00, 0.70,
        # Taper
        0.55, 0.35,
    ],
    "10K": [
        0.55, 0.65, 0.75, 0.55,
        0.80, 0.90, 0.95, 0.65,
        0.95, 1.00,
        0.65, 0.35,
    ],
    "Half Marathon": [
        0.55, 0.62, 0.70, 0.52,
        0.72, 0.82, 0.90, 0.65,
        0.92, 1.00, 0.85,
        0.65, 0.35,
    ],
    "Marathon": [
        0.55, 0.62, 0.70, 0.50,
        0.65, 0.72, 0.80, 0.60,
        0.78, 0.88, 0.95, 0.70,
        1.00, 0.92,
        0.70, 0.45,
    ],
    "Ultra (50K)": [
        0.55, 0.60, 0.68, 0.50,
        0.65, 0.72, 0.80, 0.58,
        0.78, 0.85, 0.92, 0.65,
        0.92, 1.00, 0.95, 0.65,
        0.92, 0.80,
        0.55, 0.35,
    ],
}

LONG_RUN_CURVE = {
    "5K": [
        0.55, 0.65, 0.75, 0.50,
        0.85, 1.00, 0.90, 0.65,
        0.55, 0.30,
    ],
    "10K": [
        0.55, 0.65, 0.75, 0.55,
        0.80, 0.90, 1.00, 0.65,
        0.85, 0.75,
        0.55, 0.30,
    ],
    "Half Marathon": [
        0.50, 0.60, 0.70, 0.50,
        0.75, 0.85, 0.95, 0.65,
        0.95, 1.00, 0.85,
        0.60, 0.30,
    ],
    "Marathon": [
        0.50, 0.56, 0.65, 0.45,
        0.62, 0.71, 0.80, 0.55,
        0.78, 0.88, 1.00, 0.65,
        0.96, 0.85,
        0.65, 0.30,
    ],
    "Ultra (50K)": [
        0.45, 0.55, 0.65, 0.45,
        0.65, 0.75, 0.85, 0.55,
        0.80, 0.88, 0.95, 0.65,
        0.92, 1.00, 0.95, 0.62,
        0.85, 0.72,
        0.50, 0.25,
    ],
}


def _resample(curve: List[float], target_len: int) -> List[float]:
    """Linearly interpolate a curve to a target length (in weeks)."""
    if len(curve) == target_len:
        return list(curve)
    if target_len <= 1:
        return [curve[-1]]
    out = []
    for i in range(target_len):
        # Map i in [0, target_len-1] -> position in source
        pos = i * (len(curve) - 1) / (target_len - 1)
        lo = int(pos)
        hi = min(lo + 1, len(curve) - 1)
        frac = pos - lo
        out.append(curve[lo] * (1 - frac) + curve[hi] * frac)
    return out


# ============================================================
# Phase logic
# ============================================================
def phase_for_week(week_num: int, total_weeks: int) -> str:
    """Determine training phase for a given week."""
    if week_num >= total_weeks:
        return "Race"
    # Last 2 weeks = taper
    if week_num >= total_weeks - 1:
        return "Taper"
    # Last 4 weeks (excluding taper) = peak
    if week_num >= total_weeks - 4:
        return "Peak"
    # Middle = build
    if week_num >= total_weeks // 2:
        return "Build"
    return "Base"


# ============================================================
# Plan generator (multi-race)
# ============================================================
def generate_plan(race_type: str,
                  target_time_s: int,
                  race_date: date,
                  current_weekly_km: float = 30,
                  peak_weekly_km: Optional[float] = None,
                  runs_per_week: int = 5,
                  plan_weeks: Optional[int] = None,
                  current_fitness_ctl: float = 40,
                  ) -> List[PlannedSession]:
    """
    Generate a complete race training plan for the chosen race type.

    Args:
        race_type: One of RACE_PROFILES keys ("5K", "10K", "Half Marathon", "Marathon", "Ultra (50K)")
        target_time_s: Target finish time in seconds
        race_date: Date of race
        current_weekly_km: Current avg weekly mileage
        peak_weekly_km: Target peak weekly mileage (None → use race profile default)
        runs_per_week: 3–6 running sessions per week
        plan_weeks: Length of plan (None → use race profile default)
        current_fitness_ctl: Current CTL (used to gauge starting volume)

    Returns:
        List of PlannedSession objects, day-by-day from plan_start through race day.
    """
    if race_type not in RACE_PROFILES:
        raise ValueError(f"Unknown race_type: {race_type}. "
                         f"Choose from {list(RACE_PROFILES.keys())}.")

    profile = RACE_PROFILES[race_type]
    if peak_weekly_km is None:
        peak_weekly_km = profile["default_peak_km"]
    if plan_weeks is None:
        plan_weeks = profile["default_weeks"]

    # Sanity: at minimum 4 weeks of plan
    plan_weeks = max(4, int(plan_weeks))

    paces = race_paces(race_type, target_time_s)

    # ----- ALIGN WEEKS TO MONDAY -----
    # Race week = the calendar week (Mon–Sun) that contains race_date.
    # Plan_start = Monday of (plan_weeks - 1) weeks before race week.
    # This way schedule[0]=Mon, schedule[6]=Sun maps to real weekdays,
    # and race-day override fires on the correct day regardless of weekday.
    race_monday = race_date - timedelta(days=race_date.weekday())
    plan_start = race_monday - timedelta(weeks=plan_weeks - 1)

    sessions: List[PlannedSession] = []

    vol_curve = _resample(VOLUME_CURVE[race_type], plan_weeks)
    long_curve = _resample(LONG_RUN_CURVE[race_type], plan_weeks)
    peak_long_km = profile["peak_long_km"]

    for w in range(plan_weeks):
        week_num = w + 1
        phase = phase_for_week(week_num, plan_weeks)
        vol_pct = vol_curve[w]
        long_pct = long_curve[w]

        weekly_km = peak_weekly_km * vol_pct
        long_km = peak_long_km * long_pct
        non_long_km = max(weekly_km - long_km, 0)

        week_start = plan_start + timedelta(weeks=w)  # always a Monday
        sessions_this_week = _build_week(
            week_start=week_start,
            week_num=week_num,
            phase=phase,
            race_type=race_type,
            paces=paces,
            weekly_km=weekly_km,
            long_km=long_km,
            non_long_km=non_long_km,
            runs_per_week=runs_per_week,
            race_date=race_date,
            is_race_week=(week_num == plan_weeks),
        )
        sessions.extend(sessions_this_week)

    return sessions


# Back-compat alias for any older callers
def generate_marathon_plan(target_time_s: int,
                            race_date: date,
                            current_weekly_km: float = 40,
                            peak_weekly_km: float = 75,
                            runs_per_week: int = 5,
                            plan_weeks: int = 16,
                            current_fitness_ctl: float = 50,
                            ) -> List[PlannedSession]:
    """Back-compat wrapper for marathon-only callers."""
    return generate_plan(
        race_type="Marathon",
        target_time_s=target_time_s,
        race_date=race_date,
        current_weekly_km=current_weekly_km,
        peak_weekly_km=peak_weekly_km,
        runs_per_week=runs_per_week,
        plan_weeks=plan_weeks,
        current_fitness_ctl=current_fitness_ctl,
    )


# ============================================================
# Week builder
# ============================================================
def _build_week(week_start: date, week_num: int, phase: str,
                race_type: str, paces: Dict[str, float],
                weekly_km: float, long_km: float, non_long_km: float,
                runs_per_week: int, race_date: date,
                is_race_week: bool) -> List[PlannedSession]:
    """Generate sessions for one week."""
    profile = RACE_PROFILES[race_type]
    days: List[PlannedSession] = []

    # Quality session type for the week based on phase + race
    if phase == "Base":
        # Always introduce gentle Tempo/Strides during base
        quality_type = "Tempo" if week_num >= 3 else "Strides"
    elif phase == "Build":
        quality_type = profile["build_quality"]
    elif phase == "Peak":
        quality_type = profile["peak_quality"]
    elif phase == "Taper":
        # Sharpen: short race-pace work
        quality_type = "RP" if race_type in ("5K", "10K") else "MP" if race_type == "Marathon" else "HMP"
    else:
        quality_type = "Rest"

    # Default 5-day schedule. We adapt for runs_per_week.
    schedule = {
        0: "Rest",                                # Monday
        1: quality_type,                          # Tuesday (Quality 1)
        2: "Easy",                                # Wednesday
        3: "Tempo" if phase in ("Build", "Peak") else "Easy",  # Thursday
        4: "Rest",                                # Friday
        5: "Easy" if phase != "Taper" else "Rest",  # Saturday
        6: "Long",                                # Sunday
    }

    if runs_per_week >= 6:
        schedule[0] = "Easy"
    if runs_per_week <= 4:
        # Drop Wednesday easy
        schedule[2] = "Rest"
    if runs_per_week <= 3:
        # Also drop Thursday tempo/easy
        schedule[3] = "Rest"

    # Ultra-specific tweak: in Peak phase, replace Thursday quality with a second long run (back-to-back)
    if race_type == "Ultra (50K)" and phase in ("Build", "Peak") and runs_per_week >= 5:
        schedule[5] = "Long"   # Saturday medium-long run
        schedule[3] = "Easy"

    # ----- RACE WEEK: override schedule with explicit pre-race taper -----
    # Anchor by days-to-race so it works for ANY weekday race date.
    if is_race_week:
        for day_offset in range(7):
            d = week_start + timedelta(days=day_offset)
            schedule[day_offset] = _race_week_session_for(d, race_date)

    # Distribute non-long km across easy + quality
    easy_days = [d for d, t in schedule.items() if t == "Easy"]
    quality_days = [d for d, t in schedule.items()
                    if t in ("Tempo", "Threshold", "VO2", "MP", "HMP", "RP")]

    quality_total = min(non_long_km * 0.40, 14 * max(len(quality_days), 1))
    easy_total = max(non_long_km - quality_total, 0)
    easy_per_day = easy_total / max(len(easy_days), 1) if easy_days else 0

    for day_offset in range(7):
        d = week_start + timedelta(days=day_offset)
        # Skip days BEFORE plan effectively starts (shouldn't happen normally)
        # Skip days AFTER race date so we don't keep scheduling post-race
        if d > race_date:
            break
        sess_type = schedule[day_offset]

        # Race-day override (works on any weekday)
        if is_race_week and d == race_date:
            race_pace = paces["RP"]
            race_km = profile["distance_km"]
            target_dur = race_pace * race_km
            days.append(PlannedSession(
                date=d, week_num=week_num, phase="Race",
                session_type="Race",
                distance_km=race_km,
                duration_min=round(target_dur, 0),
                target_pace_min_per_km=race_pace,
                target_hr_zone="Race effort",
                description=f"🏁 RACE DAY — {race_type}",
                workout_detail=_race_day_strategy(race_type, race_pace),
                is_quality=True,
            ))
            continue

        # Race week: handle the special "Shakeout" type from _race_week_session_for
        if is_race_week and sess_type == "Shakeout":
            # 3-4 km super-easy + 4 strides
            shake_km = 3.5
            days.append(PlannedSession(
                date=d, week_num=week_num, phase="Taper",
                session_type="Easy",
                distance_km=shake_km,
                duration_min=round(shake_km * paces["Easy"], 0),
                target_pace_min_per_km=paces["Easy"],
                target_hr_zone="Z1-Z2",
                description=f"Shakeout {shake_km:.1f} km + 4 strides",
                workout_detail=(
                    "Super-easy shakeout to keep legs loose, NOT a workout. "
                    "Pace conversational. Add 4×20-sec strides at the end (relaxed, not all-out). "
                    "Done in <30 min total."
                ),
                is_quality=False,
            ))
            continue

        if is_race_week and sess_type == "PreRaceEasy":
            # 4-6 km easy, optionally with 2-3 short strides
            pre_km = 5.0
            days.append(PlannedSession(
                date=d, week_num=week_num, phase="Taper",
                session_type="Easy",
                distance_km=pre_km,
                duration_min=round(pre_km * paces["Easy"], 0),
                target_pace_min_per_km=paces["Easy"],
                target_hr_zone="Z2",
                description=f"Easy {pre_km:.0f} km (pre-race)",
                workout_detail=(
                    "Easy aerobic run to keep blood flowing during taper. "
                    "Resist urge to test pace — race is in days, not now."
                ),
                is_quality=False,
            ))
            continue

        if is_race_week and sess_type == "RaceTuneup":
            # Mini race-pace touch: WU + 2-3 km at goal pace + CD. Only for short races.
            rp_km = 2.0 if race_type in ("5K", "10K") else 3.0
            tot = rp_km + 4.0
            days.append(PlannedSession(
                date=d, week_num=week_num, phase="Taper",
                session_type="RP",
                distance_km=tot,
                duration_min=round(2*paces["Easy"] + rp_km*paces["RP"], 0),
                target_pace_min_per_km=paces["RP"],
                target_hr_zone="Race pace",
                description=f"Race-pace tune-up: {rp_km:.0f} km @ {A.pace_to_str(paces['RP'])}",
                workout_detail=(
                    f"WU: 2 km easy + 4 strides → "
                    f"{rp_km:.0f} km @ race pace ({A.pace_to_str(paces['RP'])}) — "
                    "feel goal pace, don't grind → CD: 2 km easy. "
                    "Stop while you still feel fresh."
                ),
                is_quality=True,
            ))
            continue

        if sess_type == "Rest":
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="Rest",
                distance_km=0, duration_min=0,
                target_pace_min_per_km=None,
                target_hr_zone="—",
                description="Rest day",
                workout_detail="Full rest, OR 20-30 min cross-training (bike/swim/yoga). No running.",
                is_quality=False,
            ))
            continue

        if sess_type == "Easy":
            km = easy_per_day
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="Easy",
                distance_km=round(km, 1),
                duration_min=round(km * paces["Easy"], 0),
                target_pace_min_per_km=paces["Easy"],
                target_hr_zone="Z2",
                description=f"Easy {km:.0f} km @ {A.pace_to_str(paces['Easy'])}",
                workout_detail=(
                    "Conversational pace, Z2 HR. Should feel almost too easy. "
                    "This is your aerobic base — DON'T rush it."
                ),
                is_quality=False,
            ))
            continue

        if sess_type == "Long":
            km = long_km
            mp_seg = ""
            # Marathon: peak long runs include MP segments
            if race_type == "Marathon" and phase == "Peak" and km >= 25:
                mp_km = min(int(km * 0.35), 14)
                mp_seg = (
                    f" Include {mp_km} km @ MP ({A.pace_to_str(paces['MP'])}) "
                    "in the middle."
                )
            # Half marathon peak long run: include HMP block
            if race_type == "Half Marathon" and phase == "Peak" and km >= 16:
                hm_km = min(int(km * 0.40), 10)
                mp_seg = (
                    f" Include {hm_km} km @ HMP ({A.pace_to_str(paces['HMP'])}) "
                    "in the middle."
                )
            # Ultra: very long, mostly very easy
            if race_type == "Ultra (50K)":
                mp_seg = " Take walking breaks every 30-45 min. Practice fueling and hydration."

            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="Long",
                distance_km=round(km, 1),
                duration_min=round(km * paces["Long"], 0),
                target_pace_min_per_km=paces["Long"],
                target_hr_zone="Z2",
                description=f"Long run {km:.0f} km",
                workout_detail=(
                    f"Start easy ({A.pace_to_str(paces['Easy'])}), settle into long pace "
                    f"({A.pace_to_str(paces['Long'])}) after warm-up.{mp_seg} "
                    "Practice race-day nutrition (gel every 30-45 min)."
                ),
                is_quality=True,
            ))
            continue

        if sess_type == "Strides":
            km = easy_per_day * 0.9
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="Easy",
                distance_km=round(km, 1),
                duration_min=round(km * paces["Easy"], 0),
                target_pace_min_per_km=paces["Easy"],
                target_hr_zone="Z2",
                description=f"Easy {km:.0f} km + 6×20s strides",
                workout_detail=(
                    "Easy run, then 6×20-second strides at near-sprint pace with full recovery "
                    "jog between. Strides train neuromuscular coordination — keep them fast but relaxed."
                ),
                is_quality=False,
            ))
            continue

        if sess_type == "Tempo":
            km = max(7.0, min(non_long_km * 0.30, 14.0))
            tempo_km = round(km * 0.55, 1)
            wu = round((km - tempo_km) / 2, 1)
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="Tempo",
                distance_km=round(km, 1),
                duration_min=round(wu*paces["Easy"]*2 + tempo_km*paces["Tempo"], 0),
                target_pace_min_per_km=paces["Tempo"],
                target_hr_zone="Z3-Z4",
                description=f"Tempo: {tempo_km} km @ {A.pace_to_str(paces['Tempo'])}",
                workout_detail=(
                    f"WU: {wu} km easy → "
                    f"Tempo: {tempo_km} km @ {A.pace_to_str(paces['Tempo'])} (comfortably hard) → "
                    f"CD: {wu} km easy."
                ),
                is_quality=True,
            ))
            continue

        if sess_type == "Threshold":
            km = max(8.0, min(non_long_km * 0.32, 16.0))
            reps, rep_km = (4, 2.0) if km >= 12 else (3, 1.5)
            tot_thr = reps * rep_km
            wu = round(max((km - tot_thr) / 2 - 0.5, 1.0), 1)
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="Threshold",
                distance_km=round(km, 1),
                duration_min=round(wu*paces["Easy"]*2 + tot_thr*paces["Threshold"] + reps*1.5, 0),
                target_pace_min_per_km=paces["Threshold"],
                target_hr_zone="Z4",
                description=f"Threshold: {reps}×{rep_km} km @ {A.pace_to_str(paces['Threshold'])}",
                workout_detail=(
                    f"WU: {wu} km easy → "
                    f"{reps}×{rep_km} km @ {A.pace_to_str(paces['Threshold'])} "
                    f"(2-min jog between reps) → CD: {wu} km easy."
                ),
                is_quality=True,
            ))
            continue

        if sess_type == "VO2":
            km = max(7.0, min(non_long_km * 0.28, 12.0))
            # For 5K-focused training, use shorter intervals (400-800m)
            if race_type == "5K":
                reps = 8 if km < 9 else 10
                rep_m = 400
                rep_label = f"{reps}×400m"
                tot_vo2 = reps * 0.4
            else:
                reps = 5 if km < 10 else 6
                rep_m = 1000
                rep_label = f"{reps}×1000m"
                tot_vo2 = reps * 1.0
            wu = round(max((km - tot_vo2 - reps*0.4) / 2, 1.5), 1)
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="VO2",
                distance_km=round(km, 1),
                duration_min=round(wu*paces["Easy"]*2 + tot_vo2*paces["VO2"] + reps*2, 0),
                target_pace_min_per_km=paces["VO2"],
                target_hr_zone="Z5",
                description=f"VO2 Max: {rep_label} @ {A.pace_to_str(paces['VO2'])}",
                workout_detail=(
                    f"WU: {wu} km easy + 4 strides → "
                    f"{rep_label} @ {A.pace_to_str(paces['VO2'])} (90-sec jog rest) → "
                    f"CD: {wu} km easy."
                ),
                is_quality=True,
            ))
            continue

        if sess_type == "MP":
            km = max(10.0, min(non_long_km * 0.35, 18.0))
            mp_km = round(km * 0.65, 1)
            wu = round(max((km - mp_km) / 2, 1.0), 1)
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="MP",
                distance_km=round(km, 1),
                duration_min=round(wu*paces["Easy"]*2 + mp_km*paces["MP"], 0),
                target_pace_min_per_km=paces["MP"],
                target_hr_zone="Z3",
                description=f"Marathon pace: {mp_km} km @ {A.pace_to_str(paces['MP'])}",
                workout_detail=(
                    f"WU: {wu} km easy → {mp_km} km @ MP "
                    f"({A.pace_to_str(paces['MP'])}) → CD: {wu} km easy. "
                    f"Practice race fueling during MP segment."
                ),
                is_quality=True,
            ))
            continue

        if sess_type == "HMP":
            km = max(8.0, min(non_long_km * 0.32, 16.0))
            hmp_km = round(km * 0.55, 1)
            wu = round(max((km - hmp_km) / 2, 1.0), 1)
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="HMP",
                distance_km=round(km, 1),
                duration_min=round(wu*paces["Easy"]*2 + hmp_km*paces["HMP"], 0),
                target_pace_min_per_km=paces["HMP"],
                target_hr_zone="Z3-Z4",
                description=f"Half-marathon pace: {hmp_km} km @ {A.pace_to_str(paces['HMP'])}",
                workout_detail=(
                    f"WU: {wu} km easy → {hmp_km} km @ HMP "
                    f"({A.pace_to_str(paces['HMP'])}) → CD: {wu} km easy."
                ),
                is_quality=True,
            ))
            continue

        if sess_type == "RP":
            # Race-pace sharpening (used in taper for short races)
            km = max(5.0, min(non_long_km * 0.25, 10.0))
            rp_km = round(km * 0.45, 1)
            wu = round(max((km - rp_km) / 2, 1.0), 1)
            days.append(PlannedSession(
                date=d, week_num=week_num, phase=phase,
                session_type="RP",
                distance_km=round(km, 1),
                duration_min=round(wu*paces["Easy"]*2 + rp_km*paces["RP"], 0),
                target_pace_min_per_km=paces["RP"],
                target_hr_zone="Race pace",
                description=f"Race-pace sharpener: {rp_km} km @ {A.pace_to_str(paces['RP'])}",
                workout_detail=(
                    f"WU: {wu} km easy + 4 strides → "
                    f"{rp_km} km broken into 2-3 segments @ race pace "
                    f"({A.pace_to_str(paces['RP'])}) with 90-sec jog between → "
                    f"CD: {wu} km easy."
                ),
                is_quality=True,
            ))
            continue

    return days


def _race_week_session_for(d: date, race_date: date) -> str:
    """
    Return a special race-week session label based on days-to-race.

    Strategy (proven race-week taper):
      Race day      → "Race"        (handled by race-day override)
      Race - 1 day  → "Rest"        (legs need full rest)
      Race - 2 day  → "Shakeout"    (super easy 3-4 km + strides, keep legs loose)
      Race - 3 day  → "PreRaceEasy" (easy 4-6 km)
      Race - 4 day  → "RaceTuneup"  (short race-pace touch)
      Race - 5 day  → "PreRaceEasy"
      Race - 6 day  → "Rest"
      Race - 7+ day → "PreRaceEasy" (still race week, low volume)
      After race    → "Rest"        (post-race recovery)
    """
    if d > race_date:
        return "Rest"
    if d == race_date:
        return "Race"
    delta = (race_date - d).days
    mapping = {
        1: "Rest",
        2: "Shakeout",
        3: "PreRaceEasy",
        4: "RaceTuneup",
        5: "PreRaceEasy",
        6: "Rest",
        7: "PreRaceEasy",
    }
    return mapping.get(delta, "PreRaceEasy")


def _race_day_strategy(race_type: str, race_pace: float) -> str:
    """Per-race race-day pacing & fueling advice."""
    if race_type == "5K":
        return (
            f"WU 15 min: easy jog + 4-6 strides. "
            f"Start at GOAL pace ({A.pace_to_str(race_pace)}) — don't go out hot. "
            "Lock in for km 2-3, push the last km. No fueling needed."
        )
    if race_type == "10K":
        return (
            f"WU 15-20 min: easy + 4 strides. "
            f"First 2km @ goal pace ({A.pace_to_str(race_pace)}), settle in km 3-7, "
            "race the last 3km. Sip water if hot; no gels."
        )
    if race_type == "Half Marathon":
        return (
            f"WU 10 min easy + 4 strides. "
            f"First 3 km 3-5 sec/km slower than goal ({A.pace_to_str(race_pace)}), "
            "settle in. Take 1 gel at km 8 + 14. Race hard from km 17."
        )
    if race_type == "Marathon":
        return (
            f"Start CONSERVATIVELY (5-10 sec/km slower than MP for first 5 km). "
            f"Settle into target MP {A.pace_to_str(race_pace)} by km 8. "
            "Take gel every 30-45 min from km 8 onward. Race the last 12 km."
        )
    return (
        f"Ultra: pace by HEART RATE, not pace. Stay Z1-low Z2 until 30+ km. "
        f"Average pace target ~{A.pace_to_str(race_pace)}. "
        "Eat 60-90g carbs/hr from minute 30. Walk breaks every 30 min, walk all uphills."
    )


# ============================================================
# Daily adjustment engine
# ============================================================
@dataclass
class AdjustedSession:
    original: PlannedSession
    adjusted_type: str
    adjusted_description: str
    reason: str
    severity: str  # "none" / "minor" / "major" / "skip"


def adjust_session_for_today(planned: PlannedSession,
                              current_tsb: Optional[float],
                              ramp_rate_7d: Optional[float],
                              days_since_quality: Optional[int],
                              morning_hr: Optional[int] = None,
                              normal_resting_hr: Optional[int] = None
                              ) -> AdjustedSession:
    """
    Adjust today's planned session based on form & state.

    Decision rules (in order):
      1. Critical overload (TSB < -30 OR ramp > 8): kill quality, swap to recovery
      2. Elevated morning HR (>10% above baseline): reduce intensity
      3. Heavy fatigue (TSB < -20) on quality day: reduce volume/intensity
      4. Fresh & easy day with no recent quality: optional bonus tempo
    """
    if planned.session_type == "Rest":
        return AdjustedSession(
            original=planned,
            adjusted_type="Rest",
            adjusted_description=planned.description,
            reason="Rest day already scheduled — take it.",
            severity="none",
        )

    # Rule 1: critical overload
    if current_tsb is not None and current_tsb < -30:
        return AdjustedSession(
            original=planned,
            adjusted_type="Recovery",
            adjusted_description=(
                "🛑 Recovery only — 20-30 min very easy Z1 jog, OR full rest. "
                "Cancel scheduled workout."
            ),
            reason=f"TSB {current_tsb:+.0f} — critical fatigue, injury risk.",
            severity="major",
        )

    if ramp_rate_7d is not None and ramp_rate_7d > 8:
        return AdjustedSession(
            original=planned,
            adjusted_type="Easy",
            adjusted_description="Easy 30-40 min Z1-Z2, no intervals.",
            reason=f"CTL ramp +{ramp_rate_7d:.1f}/week — ramping too fast (injury risk).",
            severity="major",
        )

    # Rule 2: elevated morning HR
    if morning_hr and normal_resting_hr:
        if morning_hr > normal_resting_hr * 1.10:
            if planned.is_quality:
                return AdjustedSession(
                    original=planned,
                    adjusted_type="Easy",
                    adjusted_description="Replace with easy Z2 run, same duration.",
                    reason=(f"Morning HR {morning_hr} is {morning_hr - normal_resting_hr} bpm "
                            f"above normal ({normal_resting_hr}) — body not recovered."),
                    severity="major",
                )

    # Rule 3: moderate fatigue on quality day
    if current_tsb is not None and current_tsb < -20 and planned.is_quality:
        return AdjustedSession(
            original=planned,
            adjusted_type=planned.session_type,
            adjusted_description=(
                f"Reduce volume by 30%: {planned.distance_km * 0.7:.1f} km total. "
                f"Drop hardest 1-2 reps if interval session."
            ),
            reason=f"TSB {current_tsb:+.0f} — moderate fatigue, scale back quality.",
            severity="minor",
        )

    # Rule 4: TSB very positive on easy day
    if (current_tsb is not None and current_tsb > 15
            and planned.session_type == "Easy"
            and (days_since_quality or 0) >= 3):
        return AdjustedSession(
            original=planned,
            adjusted_type="Tempo",
            adjusted_description=(
                "Optional bonus: Add 20-min tempo block in the middle of your easy run. "
                "You're fresh & overdue for stimulus."
            ),
            reason=f"TSB {current_tsb:+.0f} — fresh, can absorb extra stimulus.",
            severity="minor",
        )

    # Default: execute as planned
    return AdjustedSession(
        original=planned,
        adjusted_type=planned.session_type,
        adjusted_description=planned.description,
        reason="On track. Execute as planned.",
        severity="none",
    )


# ============================================================
# Plan summary helpers
# ============================================================
def plan_summary(sessions: List[PlannedSession]) -> dict:
    """Quick stats summary for a generated plan."""
    if not sessions:
        return {}
    total_km = sum(s.distance_km for s in sessions)
    total_hrs = sum(s.duration_min for s in sessions) / 60.0
    quality_count = sum(1 for s in sessions if s.is_quality)
    by_type: Dict[str, int] = {}
    for s in sessions:
        by_type[s.session_type] = by_type.get(s.session_type, 0) + 1

    weeks = {}
    for s in sessions:
        weeks.setdefault(s.week_num, []).append(s)
    weekly_km = {w: round(sum(x.distance_km for x in lst), 1)
                 for w, lst in weeks.items()}
    peak_week_km = max(weekly_km.values()) if weekly_km else 0

    return {
        "total_sessions": len(sessions),
        "total_km": round(total_km, 1),
        "total_hours": round(total_hrs, 1),
        "quality_sessions": quality_count,
        "peak_week_km": peak_week_km,
        "weekly_km": weekly_km,
        "by_type": by_type,
        "start_date": sessions[0].date,
        "end_date": sessions[-1].date,
    }
