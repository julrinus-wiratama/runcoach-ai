"""
Analytics & coaching engine for RunCoach AI.

Implements:
- HR-based TRIMP and pace-based rTSS (Training Stress Score)
- Performance Management Chart (CTL / ATL / TSB) — Coggan model
- VO2max estimate from race-like efforts (Daniels' VDOT proxy)
- Race time predictor (Riegel formula)
- HR & pace zone distributions from streams
- Recovery time recommendation (based on TSS, IF, duration)
- Rule-based AI coach recommendations

All formulas use widely-published sport-science references:
- Coggan TSS: https://www.trainingpeaks.com/learn/articles/normalized-power-intensity-factor-training-stress/
- Riegel:    Riegel PS (1981) "Athletic Records and Human Endurance"
- Daniels VDOT: Daniels J., "Daniels' Running Formula"
- Friel zones: Joe Friel, "The Triathlete's Training Bible"
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config


# ============================================================
# Pace helpers
# ============================================================
def _is_nan(x) -> bool:
    """Safe NaN check that works for floats, np.nan, pd.NA."""
    try:
        return x != x
    except Exception:
        return False


def mps_to_pace_min_per_km(mps: Optional[float]) -> Optional[float]:
    if mps is None or _is_nan(mps) or mps <= 0:
        return None
    return (1000.0 / mps) / 60.0  # min/km


def pace_to_str(pace_min_per_km: Optional[float], suffix: str = "/km") -> str:
    if pace_min_per_km is None or _is_nan(pace_min_per_km):
        return "—"
    if pace_min_per_km <= 0 or pace_min_per_km > 30:
        return "—"
    m = int(pace_min_per_km)
    s = int(round((pace_min_per_km - m) * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}{suffix}"


def secs_to_hms(s: Optional[float]) -> str:
    if s is None or _is_nan(s) or s <= 0:
        return "—"
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def pace_axis_ticks(min_pace: float = 3.0, max_pace: float = 9.0,
                    step: float = 0.5) -> Tuple[List[float], List[str]]:
    """Return (tickvals, ticktext) for Plotly y-axes that display pace in MM:SS."""
    vals = []
    v = min_pace
    while v <= max_pace + 1e-9:
        vals.append(round(v, 2))
        v += step
    labels = [pace_to_str(v, suffix="") for v in vals]
    return vals, labels


# ============================================================
# Training Stress
# ============================================================
def hr_trimp(streams: Dict[str, list], resting_hr: int, max_hr: int, sex: str = "M") -> float:
    """
    Banister TRIMP using HR streams. Returns a scalar TRIMP score (~TSS-equivalent magnitude).
    Formula: TRIMP = sum over seconds of (dt_min * HRr * 0.64 * exp(k * HRr))
      where HRr = (HR - HRrest) / (HRmax - HRrest), k = 1.92 (M) / 1.67 (F)
    """
    hr = streams.get("heartrate") or []
    t = streams.get("time") or []
    if not hr or not t or max_hr <= resting_hr:
        return 0.0
    k = 1.92 if sex.upper() == "M" else 1.67
    total = 0.0
    for i in range(1, len(t)):
        dt = max(0.0, (t[i] - t[i - 1]) / 60.0)  # minutes
        h = hr[i] if i < len(hr) else hr[-1]
        if h is None:
            continue
        hrr = (h - resting_hr) / (max_hr - resting_hr)
        hrr = max(0.0, min(hrr, 1.2))
        total += dt * hrr * 0.64 * math.exp(k * hrr)
    return round(total, 1)


def pace_rtss(avg_speed_mps: Optional[float], moving_time_s: Optional[float],
              threshold_pace_min_per_km: float) -> Tuple[float, float]:
    """
    Pace-based running TSS (rTSS, Coggan/TrainingPeaks-style).
    Returns (rTSS, intensity_factor).
    IF = threshold_pace / actual_pace (faster = higher IF)
    rTSS = (duration_sec * NGP_speed * IF) / (FTpace_speed * 3600) * 100
         simplified to: IF^2 * duration_hours * 100
    """
    if not avg_speed_mps or avg_speed_mps <= 0 or not moving_time_s:
        return 0.0, 0.0
    actual_pace = mps_to_pace_min_per_km(avg_speed_mps)
    if not actual_pace:
        return 0.0, 0.0
    # Faster pace (lower min/km) -> higher IF
    intensity_factor = threshold_pace_min_per_km / actual_pace
    intensity_factor = max(0.4, min(intensity_factor, 1.5))
    hours = moving_time_s / 3600.0
    rtss = (intensity_factor ** 2) * hours * 100.0
    return round(rtss, 1), round(intensity_factor, 3)


def best_tss(activity_row: pd.Series, streams: Optional[Dict[str, list]],
             profile: dict) -> Tuple[float, float]:
    """Prefer HR-TRIMP when HR is available; fall back to pace rTSS."""
    if streams and streams.get("heartrate"):
        trimp = hr_trimp(streams,
                        profile.get("resting_hr") or 60,
                        profile.get("max_hr") or 190)
        # Normalize TRIMP to TSS scale: 1 hour at threshold ~= 100 TSS
        # Banister TRIMP at HRR=0.88 for 60min ≈ 120. Light scaling factor.
        rtss, intf = pace_rtss(activity_row.get("average_speed_mps"),
                               activity_row.get("moving_time_s"),
                               profile.get("threshold_pace_min_per_km") or 5.0)
        # Blend: HR is gold standard for effort; use TRIMP magnitude + IF from pace
        return round(trimp, 1), intf
    rtss, intf = pace_rtss(activity_row.get("average_speed_mps"),
                           activity_row.get("moving_time_s"),
                           profile.get("threshold_pace_min_per_km") or 5.0)
    return rtss, intf


# ============================================================
# Performance Management Chart (CTL / ATL / TSB)
# ============================================================
def compute_pmc(daily_tss: pd.Series) -> pd.DataFrame:
    """
    Given a daily-indexed TSS series (NaN = 0), compute CTL, ATL, TSB.
    CTL (42-day exponential): "fitness"
    ATL (7-day exponential):  "fatigue"
    TSB = CTL - ATL:          "form" (positive = fresh, negative = fatigued)
    """
    if daily_tss.empty:
        return pd.DataFrame(columns=["date", "tss", "ctl", "atl", "tsb"])
    s = daily_tss.fillna(0.0).astype(float).sort_index()
    ctl_alpha = 1 - math.exp(-1 / config.CTL_TIME_CONSTANT)
    atl_alpha = 1 - math.exp(-1 / config.ATL_TIME_CONSTANT)
    ctl, atl = [], []
    c_prev, a_prev = 0.0, 0.0
    for v in s.values:
        c_prev = c_prev + ctl_alpha * (v - c_prev)
        a_prev = a_prev + atl_alpha * (v - a_prev)
        ctl.append(c_prev)
        atl.append(a_prev)
    out = pd.DataFrame({
        "date": s.index,
        "tss": s.values,
        "ctl": ctl,
        "atl": atl,
    })
    out["tsb"] = out["ctl"] - out["atl"]
    return out


def build_daily_tss(activities_df: pd.DataFrame) -> pd.Series:
    if activities_df.empty:
        return pd.Series(dtype=float)
    df = activities_df.dropna(subset=["start_date"]).copy()
    df["date"] = df["start_date"].dt.tz_convert("UTC").dt.date
    daily = df.groupby("date")["tss"].sum(min_count=1)
    # Fill date range continuously
    today = datetime.now(timezone.utc).date()
    idx = pd.date_range(start=min(daily.index), end=today, freq="D").date
    daily = daily.reindex(idx).fillna(0.0)
    daily.index = pd.to_datetime(daily.index)  # tz-naive
    return daily


# ============================================================
# VO2max & race prediction
# ============================================================
def estimate_vo2max_from_race(distance_m: float, time_s: float) -> Optional[float]:
    """
    Daniels' VDOT proxy (simplified):
    velocity in m/min -> %VO2max ~= -4.6 + 0.182258*v + 0.000104*v^2
    %max  ~= 0.8 + 0.1894393*exp(-0.012778*t_min) + 0.2989558*exp(-0.1932605*t_min)
    VO2max = VO2/%max  (ml/kg/min)
    """
    if not distance_m or not time_s or distance_m < 1500:
        return None
    t_min = time_s / 60.0
    v = distance_m / t_min  # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * (v ** 2)
    pct = (0.8
           + 0.1894393 * math.exp(-0.012778 * t_min)
           + 0.2989558 * math.exp(-0.1932605 * t_min))
    if pct <= 0:
        return None
    vdot = vo2 / pct
    return round(vdot, 1)


def estimate_vo2max_from_recent(activities_df: pd.DataFrame, n_recent: int = 60) -> Optional[float]:
    """Use best-effort race-like activities in last `n_recent` days."""
    if activities_df.empty:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=n_recent)
    recent = activities_df[activities_df["start_date"] >= cutoff].copy()
    if recent.empty:
        return None
    # Pick fast efforts at >= 3 km, take top-3 by VDOT
    recent = recent[(recent["distance_m"] >= 3000) & (recent["moving_time_s"] > 0)]
    if recent.empty:
        return None
    recent["vdot"] = recent.apply(
        lambda r: estimate_vo2max_from_race(r["distance_m"], r["moving_time_s"]), axis=1)
    recent = recent.dropna(subset=["vdot"])
    if recent.empty:
        return None
    return round(recent["vdot"].nlargest(3).mean(), 1)


def riegel_predict(known_distance_m: float, known_time_s: float,
                   target_distance_m: float, exponent: float = 1.06) -> float:
    """T2 = T1 * (D2/D1)^1.06 — Riegel race-time predictor."""
    if known_distance_m <= 0 or known_time_s <= 0:
        return 0.0
    return known_time_s * ((target_distance_m / known_distance_m) ** exponent)


def best_recent_effort(activities_df: pd.DataFrame, min_distance_m: float = 5000,
                       lookback_days: int = 90) -> Optional[dict]:
    """Find fastest effort >= min_distance in the lookback window."""
    if activities_df.empty:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    df = activities_df[(activities_df["start_date"] >= cutoff)
                       & (activities_df["distance_m"] >= min_distance_m)].copy()
    if df.empty:
        return None
    df["vdot"] = df.apply(
        lambda r: estimate_vo2max_from_race(r["distance_m"], r["moving_time_s"]), axis=1)
    df = df.dropna(subset=["vdot"])
    if df.empty:
        return None
    row = df.sort_values("vdot", ascending=False).iloc[0]
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "date": row["start_date"],
        "distance_m": float(row["distance_m"]),
        "time_s": float(row["moving_time_s"]),
        "vdot": float(row["vdot"]),
    }


def predict_race_times(activities_df: pd.DataFrame) -> Dict[str, dict]:
    """Predict 5K / 10K / Half / Full from best recent effort."""
    base = best_recent_effort(activities_df)
    if not base:
        return {}
    targets = {"5K": 5000, "10K": 10000, "Half Marathon": 21097.5, "Marathon": 42195}
    out = {}
    for name, dist in targets.items():
        t = riegel_predict(base["distance_m"], base["time_s"], dist)
        out[name] = {
            "time_s": t,
            "pace_min_per_km": (t / 60.0) / (dist / 1000.0),
            "based_on": base,
        }
    return out


# ============================================================
# Zone analysis
# ============================================================
def hr_zone_distribution(streams: Dict[str, list], fthr: int) -> pd.DataFrame:
    hr = streams.get("heartrate") or []
    t = streams.get("time") or []
    if not hr or not t or fthr <= 0:
        return pd.DataFrame(columns=["zone", "seconds", "pct"])
    seconds = [0.0] * len(config.HR_ZONES)
    for i in range(1, len(t)):
        dt = max(0.0, t[i] - t[i - 1])
        h = hr[i] if i < len(hr) else hr[-1]
        if h is None:
            continue
        ratio = h / fthr
        for idx, (name, lo, hi) in enumerate(config.HR_ZONES):
            if lo <= ratio < hi:
                seconds[idx] += dt
                break
    total = sum(seconds) or 1.0
    rows = [{"zone": name, "seconds": s, "pct": round(100 * s / total, 1)}
            for (name, _, _), s in zip(config.HR_ZONES, seconds)]
    return pd.DataFrame(rows)


def pace_zone_distribution(streams: Dict[str, list], threshold_pace_min_per_km: float) -> pd.DataFrame:
    v = streams.get("velocity_smooth") or []
    t = streams.get("time") or []
    if not v or not t or threshold_pace_min_per_km <= 0:
        return pd.DataFrame(columns=["zone", "seconds", "pct"])
    seconds = [0.0] * len(config.PACE_ZONES)
    for i in range(1, len(t)):
        dt = max(0.0, t[i] - t[i - 1])
        speed = v[i] if i < len(v) else v[-1]
        if not speed or speed <= 0.2:
            continue
        pace = (1000.0 / speed) / 60.0  # min/km
        ratio = pace / threshold_pace_min_per_km  # >1 = slower than threshold
        for idx, (name, lo, hi) in enumerate(config.PACE_ZONES):
            if lo <= ratio < hi:
                seconds[idx] += dt
                break
    total = sum(seconds) or 1.0
    rows = [{"zone": name, "seconds": s, "pct": round(100 * s / total, 1)}
            for (name, _, _), s in zip(config.PACE_ZONES, seconds)]
    return pd.DataFrame(rows)


# ============================================================
# Recovery & weekly summary
# ============================================================
def recovery_hours(tss: Optional[float], intensity_factor: Optional[float],
                   moving_time_min: Optional[float]) -> float:
    """
    Recovery estimate (hours) — combines TSS and IF.
    Heuristic:
      base = TSS * 0.5
      multiplier = 1.0 (IF<=0.75) ... 1.6 (IF>=1.0)
      cap at 96h
    """
    # NaN-safe coercion
    if tss is None or _is_nan(tss):
        tss = 0.0
    if intensity_factor is None or _is_nan(intensity_factor):
        intensity_factor = 0.0
    if moving_time_min is None or _is_nan(moving_time_min):
        moving_time_min = 0.0

    if tss <= 0:
        # Fall back to time-based: easy runs ~ 1h recovery per 30 min ran
        if moving_time_min > 0:
            return round(min(96.0, moving_time_min / 30.0), 1)
        return 0.0
    base = tss * 0.5
    if intensity_factor >= 1.0:
        base *= 1.6
    elif intensity_factor >= 0.9:
        base *= 1.35
    elif intensity_factor >= 0.8:
        base *= 1.15
    return round(min(base, 96.0), 1)


def weekly_summary(activities_df: pd.DataFrame, weeks: int = 12) -> pd.DataFrame:
    if activities_df.empty:
        return pd.DataFrame()
    df = activities_df.dropna(subset=["start_date"]).copy()
    df["week_start"] = df["start_date"].dt.tz_convert("UTC").dt.to_period("W-MON").apply(
        lambda p: p.start_time)
    g = df.groupby("week_start").agg(
        runs=("id", "count"),
        distance_km=("distance_km", "sum"),
        moving_time_min=("moving_time_min", "sum"),
        elev_m=("total_elevation_gain_m", "sum"),
        tss=("tss", "sum"),
        avg_hr=("average_heartrate", "mean"),
        avg_pace=("pace_min_per_km", "mean"),
    ).reset_index().sort_values("week_start", ascending=False).head(weeks)
    return g


# ============================================================
# Rule-based AI coach
# ============================================================
@dataclass
class CoachAdvice:
    headline: str
    color: str   # "green" / "amber" / "red" / "blue"
    rationale: str
    workout: str


def coach_recommendation(pmc: pd.DataFrame, weekly: pd.DataFrame,
                         profile: dict) -> CoachAdvice:
    """
    Rule-based daily recommendation using Form (TSB) and recent ramp rate.

    TSB rules of thumb (Coggan / Friel):
      TSB > +25      : detrained — push training
      +5..+25        : fresh / race-ready
      -10..+5        : neutral, productive
      -10..-30       : building / fatigued — handle carefully
      < -30          : high overload risk

    Weekly ramp rate (CTL increase): >8/week sustained = injury risk.
    """
    if pmc.empty:
        return CoachAdvice(
            headline="Sync more runs to unlock coaching",
            color="blue",
            rationale="Not enough data yet. Sync activities from Strava to compute fitness, fatigue, and form.",
            workout="Easy 30–45 min Zone-2 run to start logging baseline data.",
        )

    last = pmc.iloc[-1]
    tsb = float(last["tsb"])
    ctl = float(last["ctl"])
    atl = float(last["atl"])

    # CTL ramp last 7 days
    ramp = 0.0
    if len(pmc) >= 8:
        ramp = ctl - float(pmc.iloc[-8]["ctl"])

    if tsb < -30 or ramp > 8:
        return CoachAdvice(
            headline="Recovery day — back off",
            color="red",
            rationale=(f"Form (TSB) is {tsb:+.0f} and 7-day CTL ramp is +{ramp:.1f}. "
                       "You're carrying high fatigue and ramping fast — injury & overtraining risk."),
            workout="Full rest, OR 20–30 min very easy Zone-1 shakeout + mobility/foam-roll. "
                    "No intervals, no long run today.",
        )

    if tsb < -10:
        return CoachAdvice(
            headline="Productive overload — keep volume, ease intensity",
            color="amber",
            rationale=(f"TSB {tsb:+.0f}, CTL {ctl:.0f}. You're in a productive build phase but fatigued. "
                       "Hold mileage steady; trim the hardest intervals."),
            workout="Easy aerobic run 45–60 min in Zone 2, or relaxed fartlek "
                    "(6×1 min strides on flat road).",
        )

    if -10 <= tsb <= 5:
        return CoachAdvice(
            headline="Sweet spot — quality day",
            color="green",
            rationale=(f"TSB {tsb:+.0f}, fitness {ctl:.0f}. Body is ready to absorb intensity."),
            workout=("Threshold workout: 15 min easy WU + 4×8 min @ threshold pace "
                     f"(~{pace_to_str(profile.get('threshold_pace_min_per_km'))}) "
                     "with 2 min jog recovery + 10 min CD."),
        )

    if 5 < tsb <= 25:
        return CoachAdvice(
            headline="Fresh & race-ready",
            color="green",
            rationale=(f"TSB {tsb:+.0f} — peaked freshness. Great window for a race, time trial, or hard key session."),
            workout="Race or time trial today, OR VO2 session: 5×3 min hard "
                    "(~95% max HR) w/ 2:30 jog. Don't waste the form on junk miles.",
        )

    # tsb > 25
    return CoachAdvice(
        headline="Detraining risk — add load",
        color="blue",
        rationale=(f"TSB {tsb:+.0f}. Fitness has plateaued/dropped (CTL {ctl:.0f}). "
                   "Time to rebuild training load."),
        workout="Long run 70–90 min Zone 2 today, plus return to 5–6 runs/week. "
                "Add a moderate tempo midweek.",
    )


def weekly_balance_notes(pace_dist: pd.DataFrame) -> List[str]:
    """80/20 rule sanity check on a single activity or aggregated week."""
    notes = []
    if pace_dist.empty:
        return notes
    easy = pace_dist[pace_dist["zone"].isin(["Z1 Easy", "Z2 Marathon"])]["pct"].sum()
    hard = pace_dist[pace_dist["zone"].isin(["Z4 Threshold", "Z5 VO2 Max", "Z6 Anaerobic"])]["pct"].sum()
    if easy + hard < 1:
        return notes
    easy_share = 100 * easy / (easy + hard) if (easy + hard) > 0 else 0
    if easy_share < 70:
        notes.append(f"Easy/Hard ratio = {easy_share:.0f}/{100 - easy_share:.0f}. "
                     "Below the 80/20 target — too much time in tempo/threshold range.")
    elif easy_share > 90:
        notes.append(f"Easy/Hard ratio = {easy_share:.0f}/{100 - easy_share:.0f}. "
                     "Very polarized toward easy — add 1 quality session per week to drive adaptation.")
    else:
        notes.append(f"Easy/Hard ratio = {easy_share:.0f}/{100 - easy_share:.0f}. "
                     "On target with the 80/20 polarized model.")
    return notes


# ============================================================
# Per-activity deep analysis (splits, decoupling, best efforts)
# ============================================================
def compute_splits(streams: Dict[str, list], split_distance_m: float = 1000.0) -> pd.DataFrame:
    """
    Compute per-split (default per-km) statistics from streams.
    Returns DataFrame with: km, time_s, pace_min_per_km, avg_hr, elev_gain_m.
    """
    t = streams.get("time") or []
    d = streams.get("distance") or []
    hr = streams.get("heartrate") or []
    alt = streams.get("altitude") or []
    if not t or not d or len(t) != len(d):
        return pd.DataFrame()

    rows = []
    split_idx = 1
    last_boundary_time = t[0]
    last_boundary_dist = d[0]
    last_boundary_alt = alt[0] if alt else 0.0
    hr_sum = 0.0
    hr_count = 0
    last_alt = last_boundary_alt
    elev_gain = 0.0
    for i in range(len(t)):
        # Accumulate HR
        if i < len(hr) and hr[i] is not None:
            hr_sum += hr[i]
            hr_count += 1
        # Accumulate elevation gain
        if alt and i < len(alt) and alt[i] is not None:
            if i > 0 and alt[i] > last_alt:
                elev_gain += alt[i] - last_alt
            last_alt = alt[i]
        # Check if we crossed a split boundary
        target_dist = last_boundary_dist + split_distance_m
        if d[i] >= target_dist:
            time_s = t[i] - last_boundary_time
            actual_dist = d[i] - last_boundary_dist
            pace = (time_s / 60.0) / (actual_dist / 1000.0) if actual_dist > 0 else None
            rows.append({
                "split": split_idx,
                "distance_km": round(actual_dist / 1000.0, 2),
                "time_s": round(time_s, 1),
                "pace_min_per_km": pace,
                "avg_hr": round(hr_sum / hr_count, 0) if hr_count else None,
                "elev_gain_m": round(elev_gain, 0),
            })
            split_idx += 1
            last_boundary_time = t[i]
            last_boundary_dist = d[i]
            hr_sum = 0.0
            hr_count = 0
            elev_gain = 0.0
    # Final partial split
    if d and d[-1] - last_boundary_dist > 100:
        time_s = t[-1] - last_boundary_time
        actual_dist = d[-1] - last_boundary_dist
        pace = (time_s / 60.0) / (actual_dist / 1000.0) if actual_dist > 0 else None
        rows.append({
            "split": split_idx,
            "distance_km": round(actual_dist / 1000.0, 2),
            "time_s": round(time_s, 1),
            "pace_min_per_km": pace,
            "avg_hr": round(hr_sum / hr_count, 0) if hr_count else None,
            "elev_gain_m": round(elev_gain, 0),
        })
    return pd.DataFrame(rows)


def aerobic_decoupling(streams: Dict[str, list]) -> Optional[float]:
    """
    Aerobic decoupling = (pace:HR ratio first half) vs (pace:HR ratio second half).
    Returns the % decoupling. <5% = good aerobic durability, >5% = needs more aerobic base.
    """
    t = streams.get("time") or []
    v = streams.get("velocity_smooth") or []
    hr = streams.get("heartrate") or []
    if not t or not v or not hr or len(t) < 60:
        return None

    mid = len(t) // 2

    def _ratio(start: int, end: int) -> Optional[float]:
        s_sum, s_n = 0.0, 0
        h_sum, h_n = 0.0, 0
        for i in range(start, end):
            if i < len(v) and v[i] and v[i] > 0.5:
                s_sum += v[i]
                s_n += 1
            if i < len(hr) and hr[i] and hr[i] > 50:
                h_sum += hr[i]
                h_n += 1
        if s_n == 0 or h_n == 0:
            return None
        return (s_sum / s_n) / (h_sum / h_n)

    r1 = _ratio(0, mid)
    r2 = _ratio(mid, len(t))
    if not r1 or not r2 or r1 <= 0:
        return None
    return round(100 * (r1 - r2) / r1, 1)


def best_efforts(streams: Dict[str, list]) -> Dict[str, dict]:
    """
    Find fastest 400m / 1K / 5K / 10K / Half / Full within a single run.
    Returns dict of {label: {time_s, pace_min_per_km}}.
    """
    t = streams.get("time") or []
    d = streams.get("distance") or []
    if not t or not d or len(t) != len(d) or d[-1] < 400:
        return {}

    targets = [("400 m", 400), ("1 km", 1000), ("5 km", 5000),
               ("10 km", 10000), ("Half Marathon", 21097.5), ("Marathon", 42195)]
    out: Dict[str, dict] = {}
    n = len(d)
    for label, dist in targets:
        if d[-1] < dist:
            continue
        best_time = None
        # Two-pointer sliding window across distance series
        j = 0
        for i in range(n):
            while j < n and d[j] - d[i] < dist:
                j += 1
            if j >= n:
                break
            time_s = t[j] - t[i]
            if best_time is None or time_s < best_time:
                best_time = time_s
        if best_time:
            out[label] = {
                "time_s": round(best_time, 1),
                "pace_min_per_km": (best_time / 60.0) / (dist / 1000.0),
            }
    return out


def hr_drift(streams: Dict[str, list]) -> Optional[float]:
    """
    Linear-fit slope of HR over time (bpm per hour). Positive = drift up.
    Useful indicator of dehydration / heat / aerobic stress on long runs.
    """
    t = streams.get("time") or []
    hr = streams.get("heartrate") or []
    if not t or not hr or len(t) < 60:
        return None
    valid = [(t[i], hr[i]) for i in range(min(len(t), len(hr)))
             if hr[i] is not None and hr[i] > 0]
    if len(valid) < 60:
        return None
    xs = np.array([v[0] for v in valid], dtype=float) / 3600.0  # hours
    ys = np.array([v[1] for v in valid], dtype=float)
    if xs.max() - xs.min() < 0.1:
        return None
    slope, _ = np.polyfit(xs, ys, 1)
    return round(float(slope), 1)


def efficiency_factor(avg_speed_mps: Optional[float],
                      avg_hr: Optional[float]) -> Optional[float]:
    """
    EF = average speed (m/min) ÷ average HR. Higher = more aerobic efficiency.
    Rising EF over time at the same easy pace = aerobic fitness improving.
    """
    if (avg_speed_mps is None or _is_nan(avg_speed_mps) or avg_speed_mps <= 0
            or avg_hr is None or _is_nan(avg_hr) or avg_hr <= 0):
        return None
    return round((avg_speed_mps * 60.0) / avg_hr, 3)


# ============================================================
# Zone reference tables (with actual HR/pace values)
# ============================================================
def zone_table_hr(fthr: int) -> pd.DataFrame:
    """Render HR zones as a table with actual bpm ranges based on user's FTHR."""
    rows = []
    for name, lo, hi in config.HR_ZONES:
        hr_lo = int(round(lo * fthr))
        hr_hi = int(round(hi * fthr))
        feel, desc = config.HR_ZONE_INFO.get(name, ("", ""))
        if hi >= 1.5:
            hr_range = f"> {hr_lo} bpm"
        elif lo == 0:
            hr_range = f"< {hr_hi} bpm"
        else:
            hr_range = f"{hr_lo} – {hr_hi} bpm"
        rows.append({
            "Zone": name,
            "% FTHR": f"{int(lo*100)} – {int(hi*100)}%" if hi < 1.5 else f"> {int(lo*100)}%",
            "HR range": hr_range,
            "Feel": feel,
            "What it's for": desc,
        })
    return pd.DataFrame(rows)


def zone_table_pace(thr_pace_min_per_km: float) -> pd.DataFrame:
    """Render pace zones as a table with actual pace ranges (MM:SS/km)."""
    rows = []
    for name, lo_mult, hi_mult in config.PACE_ZONES:
        pace_fast = lo_mult * thr_pace_min_per_km  # faster (lower min/km)
        pace_slow = hi_mult * thr_pace_min_per_km
        feel, desc = config.PACE_ZONE_INFO.get(name, ("", ""))
        if hi_mult > 50:  # Z1 has no upper bound (slowest)
            pace_str = f"slower than {pace_to_str(pace_fast, '')}/km"
            mult_str = f"> {lo_mult:.2f}×"
        elif lo_mult == 0:
            pace_str = f"faster than {pace_to_str(pace_slow, '')}/km"
            mult_str = f"< {hi_mult:.2f}×"
        else:
            pace_str = f"{pace_to_str(pace_fast, '')} – {pace_to_str(pace_slow, '')}/km"
            mult_str = f"{lo_mult:.2f} – {hi_mult:.2f}×"
        rows.append({
            "Zone": name,
            "Multiplier": mult_str,
            "Pace range": pace_str,
            "Feel": feel,
            "What it's for": desc,
        })
    return pd.DataFrame(rows)


# ============================================================
# Auto-detect threshold pace + FTHR from historical data
# ============================================================
def best_continuous_pace(streams: Dict[str, list], duration_s: float
                         ) -> Optional[Tuple[float, Optional[float]]]:
    """
    Find best (fastest) avg pace over `duration_s` continuous window in ONE activity.
    Also computes avg HR during that window. Returns (pace_min_per_km, avg_hr_bpm).
    Uses two-pointer sliding window over the time stream.
    """
    t = streams.get("time") or []
    d = streams.get("distance") or []
    hr = streams.get("heartrate") or []
    if not t or not d or len(t) != len(d):
        return None
    if t[-1] - t[0] < duration_s * 0.95:
        return None  # activity too short

    n = len(t)
    best_dist = 0.0
    best_window = None  # (i, j)

    j = 0
    for i in range(n):
        while j < n and t[j] - t[i] < duration_s:
            j += 1
        if j >= n:
            break
        actual_dur = t[j] - t[i]
        # Only consider windows that are close to the target duration
        if actual_dur < duration_s * 0.95 or actual_dur > duration_s * 1.10:
            continue
        dist = d[j] - d[i]
        if dist > best_dist:
            best_dist = dist
            best_window = (i, j)

    if not best_window or best_dist <= 0:
        return None

    i, j = best_window
    pace = (duration_s / 60.0) / (best_dist / 1000.0)

    # Average HR in that window
    hr_window = [hr[k] for k in range(i, j) if k < len(hr) and hr[k] and hr[k] > 50]
    avg_hr = sum(hr_window) / len(hr_window) if hr_window else None

    return (pace, avg_hr)


def auto_detect_thresholds(activities_df: pd.DataFrame,
                            get_streams_func,
                            lookback_days: int = 180,
                            target_duration_s: float = 1800,
                            min_activity_dist_m: float = 5000
                            ) -> Optional[Dict[str, object]]:
    """
    Scan recent activities for the best sustained ~30-min effort.
    Returns suggested threshold values + the activity it was based on.

    Methodology:
      - Best 30-min avg pace ≈ threshold pace (Coggan / Daniels' "T-pace")
      - Avg HR during that effort ≈ FTHR (functional threshold HR / LTHR)
      - Both have been used by exercise scientists as proxies for the lactate threshold
        without needing a lab test.
    """
    if activities_df.empty:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    recent = activities_df[
        (activities_df["start_date"] >= cutoff)
        & (activities_df["moving_time_s"] >= target_duration_s)
        & (activities_df["distance_m"] >= min_activity_dist_m)
    ].copy()
    if recent.empty:
        return None

    best = {"pace": None, "hr": None, "id": None, "name": None, "date": None}
    candidates_count = 0

    for _, row in recent.iterrows():
        streams = get_streams_func(int(row["id"]))
        if not streams:
            continue
        result = best_continuous_pace(streams, target_duration_s)
        if not result:
            continue
        pace, hr_avg = result
        candidates_count += 1
        if best["pace"] is None or pace < best["pace"]:
            best.update({
                "pace": pace,
                "hr": hr_avg,
                "id": int(row["id"]),
                "name": row["name"],
                "date": row["start_date"],
            })

    if best["pace"] is None:
        return None

    return {
        "threshold_pace_min_per_km": round(best["pace"], 2),
        "fthr": int(round(best["hr"])) if best["hr"] else None,
        "based_on_activity_id": best["id"],
        "based_on_activity_name": best["name"],
        "based_on_date": best["date"],
        "candidates_count": candidates_count,
        "duration_s": target_duration_s,
    }


def estimate_max_hr_from_data(activities_df: pd.DataFrame,
                              lookback_days: int = 365) -> Optional[int]:
    """
    Find max HR ever recorded in recent activities. Usually this is the user's
    actual max HR (or very close to it) — much more accurate than 220-age.
    """
    if activities_df.empty:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    recent = activities_df[activities_df["start_date"] >= cutoff]
    if recent.empty or "max_heartrate" not in recent.columns:
        return None
    valid = recent["max_heartrate"].dropna()
    if valid.empty:
        return None
    # Take 99th percentile to filter out single anomalous spikes
    max_hr = float(valid.quantile(0.99))
    return int(round(max_hr))
