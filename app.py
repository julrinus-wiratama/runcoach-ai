"""
RunCoach AI — Streamlit dashboard
Run with: streamlit run app.py

Multi-user: setiap data discope ke Strava athlete_id. Connect Strava =
otomatis "login" (gak perlu password terpisah). Untuk pakai bareng pacar
atau teman, mereka cukup buka URL yang sama dan klik "Connect Strava"
dengan akun masing-masing — data 100% terpisah.
"""
from __future__ import annotations

import json as _json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import analytics as A
import config
import database as db
import strava_client as sc
import training_plan as TP


# ---------------------------------------------------------
# Page setup
# ---------------------------------------------------------
st.set_page_config(
    page_title="RunCoach AI",
    page_icon="🏃",
    layout="wide",
    initial_sidebar_state="expanded",
)

db.init_db()


# Inject a tiny CSS polish
st.markdown(
    """
    <style>
      .metric-card { padding: 1rem; border-radius: 10px;
                     background: #f7f9fc; border: 1px solid #e2e8f0; }
      .coach-card  { padding: 1.2rem 1.4rem; border-radius: 12px;
                     border-left: 6px solid #3b82f6; background: #f8fafc;
                     margin-bottom: 1rem; }
      .coach-card.green { border-left-color: #10b981; }
      .coach-card.amber { border-left-color: #f59e0b; }
      .coach-card.red   { border-left-color: #ef4444; }
      .coach-card.blue  { border-left-color: #3b82f6; }
      h1, h2, h3 { font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------
# Multi-user session helpers
# ---------------------------------------------------------
def current_user_id() -> Optional[int]:
    """
    Return current logged-in Strava athlete_id, or None.

    SECURITY: Hanya session_state yang valid sebagai sumber identity.
    URL query param (?u=...) TIDAK boleh dipakai — itu impersonation vector
    (siapa pun yang punya URL bisa pretend jadi user lain).
    Session_state cuma di-set lewat OAuth callback yang valid.
    """
    uid = st.session_state.get("user_id")
    if uid is not None:
        try:
            return int(uid)
        except (ValueError, TypeError):
            pass
    return None


def is_authenticated() -> bool:
    return current_user_id() is not None


def athlete_name_for(user_id: int, fallback: str = "Athlete") -> str:
    u = db.get_user(user_id)
    if not u or not u.get("athlete_json"):
        # try tokens table
        tok = db.get_tokens(user_id)
        if tok and tok.get("athlete_json"):
            try:
                a = _json.loads(tok["athlete_json"])
                name = f"{a.get('firstname','')} {a.get('lastname','')}".strip()
                return name or fallback
            except Exception:
                pass
        return fallback
    try:
        a = _json.loads(u["athlete_json"])
        name = f"{a.get('firstname','')} {a.get('lastname','')}".strip()
        return name or fallback
    except Exception:
        return fallback


# ---------------------------------------------------------
# Handle Strava OAuth redirect (?code=...)
# ---------------------------------------------------------
def handle_oauth_callback():
    qp = st.query_params
    code = qp.get("code")
    if code:
        try:
            data = sc.exchange_code_for_token(code)
            user_id = data["athlete_id"]
            st.session_state["user_id"] = user_id

            # Kalau ada data legacy (dari schema single-user lama, user_id=0),
            # auto-claim ke user yang baru OAuth (kemungkinan besar dia pemilik aslinya)
            if db.has_orphan_data():
                moved = db.claim_orphan_data(user_id)
                total_moved = sum(v for v in moved.values() if isinstance(v, int))
                if total_moved > 0:
                    st.info(
                        f"📦 Data dari sesi sebelumnya berhasil dipindahkan ke akun Anda "
                        f"({total_moved} row dipindah). Lihat 'Data migration' di bawah untuk detail."
                    )
                    with st.expander("Data migration details"):
                        st.json(moved)

            # Bersihin URL — JANGAN simpan user_id di URL (impersonation vector)
            st.query_params.clear()
            st.success(f"Berhasil connect ke Strava sebagai **{athlete_name_for(user_id)}**! "
                       "Klik 'Sync activities' di sidebar buat narik lari Anda.")
        except Exception as e:
            st.error(f"OAuth failed: {e}")


handle_oauth_callback()


# ---------------------------------------------------------
# Landing page — shown when no user logged in
# ---------------------------------------------------------
def show_landing():
    st.title("🏃 RunCoach AI")
    st.caption("Strava-powered training analytics & adaptive race plan")

    st.markdown(
        """
        Selamat datang! App ini akan narik data lari Anda dari Strava
        dan kasih analisis training load, zone breakdown, race prediction,
        AI coach, dan training plan adaptive.

        Setiap user pakai akun Strava masing-masing — datanya **100% terpisah**
        dan hanya bisa diakses dengan login Strava akun itu.
        """
    )

    creds_ok = bool(config.STRAVA_CLIENT_ID and config.STRAVA_CLIENT_SECRET)
    if not creds_ok:
        st.error("Strava API keys belum di-set. Admin perlu konfigurasi secrets dulu.")
        st.stop()

    st.divider()
    st.link_button("🚴 Connect with Strava", sc.build_authorize_url(),
                   use_container_width=True, type="primary")
    st.caption(
        "Anda akan diarahkan ke Strava untuk authorize akses read-only ke aktivitas. "
        "Kalau sudah pernah authorize sebelumnya, Strava akan langsung redirect "
        "kembali ke sini (1 klik). Tidak perlu password tambahan — identitas Anda "
        "dijamin oleh login Strava."
    )


# Gate: kalau gak ada user, tampilin landing dan stop
if not is_authenticated():
    show_landing()
    st.stop()


# ---------------------------------------------------------
# Profile bootstrap — for current user
# ---------------------------------------------------------
_uid = current_user_id()
if not db.get_profile(_uid):
    db.save_profile(_uid, config.DEFAULT_PROFILE)


def current_profile() -> dict:
    return db.get_profile(current_user_id()) or config.DEFAULT_PROFILE


# ---------------------------------------------------------
# Data loaders (cached)
# ---------------------------------------------------------
@st.cache_data(ttl=600, show_spinner="Loading your runs…")
def _load_runs_for_user(user_id: int) -> pd.DataFrame:
    """
    Load runs untuk user. FAST path: pakai TSS/IF yang sudah cached di DB.
    Lazy compute: kalau ada baris yang TSS-nya NULL, compute on-demand &
    persist ke DB biar load berikutnya gak perlu compute lagi.
    """
    df = db.get_activities_df(user_id, sport_filter="Run")
    if df.empty:
        return df

    # Fast pace (no DB hit needed, derived from average_speed_mps)
    if "average_pace_min_per_km" not in df.columns or df["average_pace_min_per_km"].isna().any():
        df["average_pace_min_per_km"] = df["average_speed_mps"].apply(A.mps_to_pace_min_per_km)

    # VO2max estimate (no DB hit needed)
    if "estimated_vo2max" not in df.columns or df["estimated_vo2max"].isna().any():
        df["estimated_vo2max"] = df.apply(
            lambda r: A.estimate_vo2max_from_race(r.get("distance_m"), r.get("moving_time_s")),
            axis=1,
        )

    # Lazy TSS/IF compute (only for rows where tss is NULL) + persist to DB
    missing_tss = df[df["tss"].isna()] if "tss" in df.columns else df
    if not missing_tss.empty:
        profile = db.get_profile(user_id) or config.DEFAULT_PROFILE
        for idx, row in missing_tss.iterrows():
            streams = db.get_streams(int(row["id"]))
            t, intf = A.best_tss(row, streams, profile)
            df.at[idx, "tss"] = t
            df.at[idx, "intensity_factor"] = intf
            # Persist ke DB biar load berikutnya skip compute.
            # Wrap di try/except: kalau UPDATE gagal, cache miss OK,
            # app tetap jalan (cuma load berikutnya lebih lambat).
            try:
                db.update_activity_metrics(user_id, int(row["id"]), {
                    "tss": float(t) if t is not None else None,
                    "intensity_factor": float(intf) if intf is not None else None,
                    "average_pace_min_per_km": (
                        float(df.at[idx, "average_pace_min_per_km"])
                        if pd.notna(df.at[idx, "average_pace_min_per_km"]) else None
                    ),
                    "estimated_vo2max": (
                        float(df.at[idx, "estimated_vo2max"])
                        if pd.notna(df.at[idx, "estimated_vo2max"]) else None
                    ),
                    "normalized_pace_min_per_km": None,
                })
            except Exception:
                pass  # Best-effort cache; failure non-fatal
    return df


def load_runs() -> pd.DataFrame:
    """Convenience wrapper — loads runs for the currently authenticated user."""
    uid = current_user_id()
    if uid is None:
        return pd.DataFrame()
    return _load_runs_for_user(uid)


def clear_caches():
    _load_runs_for_user.clear()


# ---------------------------------------------------------
# Sidebar — Sync & navigation
# ---------------------------------------------------------
with st.sidebar:
    st.title("🏃 RunCoach AI")
    st.caption("Strava → analytics → coach")

    uid = current_user_id()
    athlete_name = athlete_name_for(uid)
    st.success(f"Connected: {athlete_name}")

    if st.button("🔄 Sync activities", use_container_width=True, type="primary"):
        with st.spinner("Pulling from Strava…"):
            pbar = st.progress(0, text="Starting sync…")
            done = [0]

            def cb(n, name):
                done[0] = n
                pbar.progress(min(n / 50, 1.0), text=f"Synced {n}: {name[:40]}")

            try:
                n = sc.sync_activities(uid, progress_cb=cb)
                pbar.empty()
                if n:
                    st.success(f"Synced {n} new activities.")
                else:
                    st.info("No new activities — you're up to date.")
                clear_caches()
            except Exception as e:
                st.error(f"Sync failed: {e}")

    col_logout, col_disc = st.columns(2)
    with col_logout:
        if st.button("Switch user", use_container_width=True,
                     help="Logout dari user ini, kembali ke landing page."):
            st.session_state.pop("user_id", None)
            try:
                st.query_params.clear()
            except Exception:
                pass
            st.rerun()
    with col_disc:
        if st.button("Disconnect", use_container_width=True,
                     help="Hapus token Strava untuk user ini (perlu re-OAuth)."):
            db.clear_tokens(uid)
            st.session_state.pop("user_id", None)
            try:
                st.query_params.clear()
            except Exception:
                pass
            st.rerun()

    st.divider()
    page = st.radio(
        "Pages",
        [
            "🏠 Overview",
            "📈 Training Load (PMC)",
            "🎯 Zone Analysis",
            "🏁 Race Predictor",
            "🧠 AI Coach",
            "🗓️ Training Plan",
            "🔍 Activity Detail",
            "⚙️ Settings",
        ],
        label_visibility="collapsed",
    )

    st.divider()
    total = db.count_activities(uid)
    st.caption(f"📊 {total} activities in database")


# ---------------------------------------------------------
# Helpers for headline metrics
# ---------------------------------------------------------
def kpi(col, label: str, value: str, delta: str = "", help_text: str = ""):
    """
    Render a KPI metric using Streamlit's native st.metric.
    `help_text` becomes the (?) hover tooltip — a real clickable popup.
    """
    with col:
        st.metric(
            label=label,
            value=value,
            delta=delta if delta else None,
            delta_color="off",  # neutral gray for context, no green/red trend coloring
            help=help_text if help_text else None,
        )


def explain(title: str, body: str):
    """Render a 'Apa artinya?' expander for layperson explanations."""
    with st.expander(f"💡 {title}"):
        st.markdown(body)


# ---------------------------------------------------------
# PAGE: Overview
# ---------------------------------------------------------
def page_overview():
    st.title("Overview")
    df = load_runs()
    if df.empty:
        st.info("Connect to Strava and sync activities to see your dashboard.")
        return

    st.info(
        "👋 **Halaman ini ringkasan kondisi lari Anda saat ini.** "
        "4 angka utama di bawah = volume latihan terbaru + kebugaran Anda sekarang. "
        "Hover ⓘ di sebelah label untuk penjelasan tiap metric."
    )

    today = datetime.now(timezone.utc)
    last_7  = df[df["start_date"] >= today - timedelta(days=7)]
    last_28 = df[df["start_date"] >= today - timedelta(days=28)]
    last_365 = df[df["start_date"] >= today - timedelta(days=365)]

    profile = current_profile()
    pmc = A.compute_pmc(A.build_daily_tss(df))
    last_pmc = pmc.iloc[-1] if not pmc.empty else None
    vdot = A.estimate_vo2max_from_recent(df)

    c1, c2, c3, c4 = st.columns(4)
    kpi(c1, "Runs (7d)", f"{len(last_7)}",
        f"{last_7['distance_km'].sum():.1f} km",
        help_text="Jumlah lari Anda 7 hari terakhir. Untuk runner amatir ideal 3-5 sesi/minggu; lebih dari itu butuh recovery yang lebih baik.")
    kpi(c2, "Distance (28d)", f"{last_28['distance_km'].sum():.0f} km",
        f"{last_28['moving_time_min'].sum()/60:.1f} h moving",
        help_text="Total jarak 28 hari terakhir. Konsistensi volume per bulan lebih penting daripada satu minggu yang gila-gilaan.")
    kpi(c3, "Fitness (CTL)",
        f"{last_pmc['ctl']:.0f}" if last_pmc is not None else "—",
        f"Form (TSB) {last_pmc['tsb']:+.0f}" if last_pmc is not None else "",
        help_text="Skor kebugaran Anda. Naik perlahan kalau latihan konsisten 4-6 minggu. Patokan: 40-60 = casual runner, 60-90 = serious amateur, 90+ = competitive.")
    kpi(c4, "Est. VO₂max", f"{vdot}" if vdot else "—",
        "ml/kg/min — Daniels VDOT",
        help_text="Estimasi kapasitas aerobik max berdasarkan run terbaik 60 hari terakhir. Patokan pria: 45+ baik, 50+ sangat baik, 60+ elit. Wanita: kurangin ~5.")

    explain(
        "Bagaimana cara baca angka-angka ini?",
        """
**Runs (7d) & Distance (28d)** — ini volume kasar. Cek konsistensi: kalau biasanya 50 km/bulan lalu tiba-tiba 100 km, itu lompatan terlalu cepat = risiko cedera.

**Fitness (CTL)** — angka ajaib dari training science. Anggap aja seperti "saldo tabungan kebugaran" Anda. Tiap lari nambah saldo, tiap istirahat saldo turun dikit. Naiknya pelan (1-3 poin/minggu yang sehat).

**VO₂max** — kemampuan tubuh menyerap & pakai oksigen. Atlet elit punya VO₂max 70-85; runner amatir yang fit 50-60. Naik 2-5 poin per tahun kalau latihan serius. Angka ini bukan dari tes lab — diestimasi dari kecepatan run terbaik Anda pakai formula Jack Daniels yang dipakai pelatih pro sejak 1979.
        """
    )

    st.divider()

    # Weekly mileage bar
    weekly = A.weekly_summary(df, weeks=16)
    if not weekly.empty:
        weekly = weekly.sort_values("week_start")
        fig = px.bar(weekly, x="week_start", y="distance_km",
                     title="Weekly mileage (last 16 weeks)",
                     labels={"week_start": "Week", "distance_km": "Distance (km)"})
        fig.update_traces(marker_color="#3b82f6")
        fig.update_layout(height=320, margin=dict(t=50, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "💡 Cari pola: minggu-minggu di mana bar tiba-tiba jauh lebih tinggi dari rata-rata "
            "= rawan injury. Aturan tradisional: jangan naikin volume lebih dari 10% per minggu."
        )

    # Recent activities table
    st.subheader("Recent activities")
    recent = df.head(15).copy()
    recent["Date"] = recent["start_date_local"].dt.strftime("%Y-%m-%d %a")
    recent["Distance"] = recent["distance_km"].map(lambda v: f"{v:.2f} km")
    recent["Time"] = recent["moving_time_s"].map(A.secs_to_hms)
    recent["Pace"] = recent["pace_min_per_km"].map(A.pace_to_str)
    recent["HR"] = recent["average_heartrate"].map(
        lambda v: f"{v:.0f}" if pd.notna(v) else "—")
    recent["TSS"] = recent["tss"].map(lambda v: f"{v:.0f}" if pd.notna(v) else "—")
    recent["IF"] = recent["intensity_factor"].map(
        lambda v: f"{v:.2f}" if pd.notna(v) and v > 0 else "—")
    st.dataframe(
        recent[["Date", "name", "Distance", "Time", "Pace", "HR", "TSS", "IF"]]
        .rename(columns={"name": "Activity"}),
        hide_index=True, use_container_width=True,
    )
    st.caption(
        "💡 **TSS** = beratnya latihan (100 = 1 jam habis-habisan di ambang). "
        "**IF** = intensitas (1.0 = race pace, 0.7-0.8 = easy, 0.9+ = tempo/threshold). "
        "Klik halaman 🔍 Activity Detail untuk deep analysis per lari."
    )


# ---------------------------------------------------------
# PAGE: Training Load (PMC)
# ---------------------------------------------------------
def page_pmc():
    st.title("Training Load — Performance Management Chart")
    st.caption("CTL = fitness · ATL = fatigue · TSB = form (CTL − ATL)")
    df = load_runs()
    if df.empty:
        st.info("No data yet.")
        return

    st.info(
        "📊 **Chart ini dipakai pelatih pro buat mantau atlet.** "
        "Intinya nampilin 3 kurva: berapa fit Anda (biru), berapa capek Anda (merah), dan "
        "berapa siap Anda balapan (hijau). Kalau Anda mau target race serius, halaman ini "
        "yang paling penting buat ngatur peaking dan taper."
    )

    pmc = A.compute_pmc(A.build_daily_tss(df))
    if pmc.empty:
        st.info("Need more training data.")
        return

    window = st.select_slider("Window", options=[30, 60, 90, 180, 365, 999], value=180,
                              format_func=lambda v: "All time" if v == 999 else f"{v} days")
    # Make cutoff tz-naive to match pmc["date"] (which comes from datetime.date)
    cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=window)
    view = pmc[pmc["date"] >= cutoff] if window != 999 else pmc

    fig = go.Figure()
    fig.add_bar(x=view["date"], y=view["tss"], name="Daily TSS",
                marker_color="#cbd5e1", yaxis="y2", opacity=0.6)
    fig.add_scatter(x=view["date"], y=view["ctl"], name="Fitness (CTL)",
                    line=dict(color="#3b82f6", width=3))
    fig.add_scatter(x=view["date"], y=view["atl"], name="Fatigue (ATL)",
                    line=dict(color="#ef4444", width=2, dash="dot"))
    fig.add_scatter(x=view["date"], y=view["tsb"], name="Form (TSB)",
                    line=dict(color="#10b981", width=2))
    fig.add_hline(y=0, line_dash="dash", line_color="#94a3b8", yref="y")
    fig.update_layout(
        height=480,
        yaxis=dict(title="CTL / ATL / TSB"),
        yaxis2=dict(title="Daily TSS", overlaying="y", side="right", showgrid=False),
        legend=dict(orientation="h", y=1.05),
        margin=dict(t=50, b=10, l=10, r=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    last = pmc.iloc[-1]
    c1, c2, c3 = st.columns(3)
    kpi(c1, "Fitness (CTL)", f"{last['ctl']:.0f}",
        "Long-term training load (42-day EMA)",
        help_text="Rata-rata 42 hari TSS Anda. Mewakili kondisi fit Anda. Naik perlahan, butuh konsistensi berminggu-minggu.")
    kpi(c2, "Fatigue (ATL)", f"{last['atl']:.0f}",
        "Recent training load (7-day EMA)",
        help_text="Rata-rata 7 hari TSS. Mewakili kelelahan akut. Naik cepat setelah hard workout, turun cepat setelah istirahat.")
    tsb = last["tsb"]
    interp = ("Fresh — race-ready" if tsb > 5
              else "Productive — building" if tsb > -10
              else "Fatigued — handle with care" if tsb > -30
              else "Overload — back off")
    kpi(c3, "Form (TSB)", f"{tsb:+.0f}", interp,
        help_text="CTL minus ATL = seberapa fresh Anda. Positif = badan udah pulih dari latihan. Negatif = masih nyimpan capek dari hari-hari sebelumnya.")

    explain(
        "Apa artinya 3 kurva ini? (analogi mudah)",
        """
Bayangin Anda punya **rekening bank kebugaran**:

- **Fitness (CTL)** 🔵 — saldo Anda di bank. Naik tiap kali Anda lari (setoran), turun pelan kalau Anda gak lari (saldo terkikis biaya admin). Karena ini "rata-rata 42 hari", angka ini gak goyang harian — perlu konsistensi minggu-mingguan.

- **Fatigue (ATL)** 🔴 — utang sementara Anda. Tiap hard workout = ngutang capek. Tidur & rest day = bayar utang. Ini lebih reaktif — 1 minggu rest udah turun banyak.

- **Form (TSB)** 🟢 — selisih saldo dikurangin utang = uang yang bener-bener bisa dipakai. Kalau positif (+5 sampai +25), badan Anda fresh & siap balapan/PR. Kalau negatif dalam (-20 ke bawah), Anda ngutang banyak — risiko cedera.

**Strategi balapan klasik:** 3-4 minggu sebelum race, naikin CTL serajin mungkin (TSB akan jeblok negatif, normal). 2 minggu sebelum race mulai **taper** (kurangin volume sekitar 30-40%). TSB akan rebound jadi positif. Race di TSB sekitar +10 sampai +20 = sweet spot peak performance.

**Patokan TSB:**
- **TSB > +25** → terlalu lama rest, fitness mulai luntur
- **+5 sampai +25** → fresh, race-ready ⭐
- **−10 sampai +5** → latihan produktif, badan masih bisa adaptasi
- **−10 sampai −30** → capek serius, hati-hati
- **< −30** → overload, super risky cedera

**Patokan ramp CTL** (kenaikan CTL per minggu): sekitar +3 sampai +7/minggu sustainable. >+8/minggu konsisten = lampu kuning cedera.
        """
    )


# ---------------------------------------------------------
# PAGE: Zone Analysis
# ---------------------------------------------------------
def page_zones():
    st.title("Zone Analysis")
    df = load_runs()
    if df.empty:
        st.info("No data yet.")
        return

    st.info(
        "🎯 **Halaman ini ngeliat berapa lama Anda di tiap intensitas.** "
        "Riset menunjukkan runner elit lari 80% di intensitas EASY dan cuma 20% di HARD — bukan rata "
        "tengah-tengah. Banyak amatir terjebak di zona tempo (Z3) yang \"medioker\" — terlalu hard "
        "untuk recovery tapi terlalu easy untuk bikin adaptasi. Tujuan halaman ini: cek apakah Anda "
        "udah ikutin pola polarisasi yang bener."
    )

    profile = current_profile()
    fthr = profile.get("fthr") or 170
    thr_pace = profile.get("threshold_pace_min_per_km") or 5.0

    # ----------- Zone reference tables -----------
    st.subheader("📋 Your zone definitions")
    st.caption(
        f"Berdasarkan setting Anda: **FTHR = {fthr} bpm**, "
        f"**Threshold pace = {A.pace_to_str(thr_pace)}**. "
        "Ganti di halaman ⚙️ Settings kalau angka ini belum akurat."
    )
    tab_hr, tab_pace = st.tabs(["HR Zones (bpm)", "Pace Zones (min/km)"])
    with tab_hr:
        hr_zones_df = A.zone_table_hr(fthr)
        st.dataframe(hr_zones_df, hide_index=True, use_container_width=True)
    with tab_pace:
        pace_zones_df = A.zone_table_pace(thr_pace)
        st.dataframe(pace_zones_df, hide_index=True, use_container_width=True)

    # ----------- Auto-detect thresholds -----------
    with st.expander("🎯 **Auto-detect threshold dari data lari Anda** (recommended kalau setting belum yakin)"):
        st.markdown(
            "Sistem akan **scan semua lari Anda** untuk cari best 30-menit sustained effort. "
            "Pace + HR di effort itu = estimasi threshold pace + FTHR yang biasanya jauh "
            "lebih akurat daripada nebak manual."
        )
        c1, c2 = st.columns([1, 3])
        with c1:
            do_detect = st.button("🔍 Scan data saya", type="primary")
        with c2:
            st.caption("Scan 180 hari terakhir. Butuh ~30 detik buat 500+ aktivitas.")

        if do_detect:
            with st.spinner("Scanning sustained efforts across activities..."):
                result = A.auto_detect_thresholds(
                    df, db.get_streams, lookback_days=180, target_duration_s=1800
                )
                max_hr_est = A.estimate_max_hr_from_data(df)

            if not result:
                st.warning(
                    "Tidak ada lari ≥30 menit yang punya stream HR/pace di 180 hari terakhir. "
                    "Tambah window jadi 365 hari di kode, atau pastikan watch Anda record HR + GPS."
                )
            else:
                st.success(
                    f"✅ Scan selesai! Best 30-min effort ditemukan dari "
                    f"**{result['based_on_activity_name']}** ({result['based_on_date'].strftime('%d %b %Y')}) "
                    f"— scanned dari {result['candidates_count']} aktivitas yang memenuhi syarat."
                )
                c1, c2, c3 = st.columns(3)
                with c1:
                    new_pace = result['threshold_pace_min_per_km']
                    delta_pace = new_pace - thr_pace
                    arrow = "🔻 lebih cepat" if delta_pace < -0.05 else "🔺 lebih lambat" if delta_pace > 0.05 else "≈ sama"
                    st.metric(
                        "Suggested Threshold pace",
                        A.pace_to_str(new_pace),
                        f"vs setting Anda: {A.pace_to_str(thr_pace)} ({arrow})",
                        delta_color="off",
                        help="Pace rata-rata di 30 menit tercepat Anda. Itulah threshold pace asli Anda."
                    )
                with c2:
                    if result["fthr"]:
                        new_fthr = result["fthr"]
                        delta_fthr = new_fthr - fthr
                        arrow = "🔺 lebih tinggi" if delta_fthr > 2 else "🔻 lebih rendah" if delta_fthr < -2 else "≈ sama"
                        st.metric(
                            "Suggested FTHR",
                            f"{new_fthr} bpm",
                            f"vs setting Anda: {fthr} bpm ({arrow})",
                            delta_color="off",
                            help="HR rata-rata saat 30 menit tercepat Anda. Itulah FTHR asli Anda."
                        )
                    else:
                        st.metric("Suggested FTHR", "—", "No HR data in best effort")
                with c3:
                    if max_hr_est:
                        cur_max = profile.get("max_hr") or 0
                        st.metric(
                            "Estimated Max HR",
                            f"{max_hr_est} bpm",
                            f"current setting: {cur_max} bpm",
                            delta_color="off",
                            help="HR tertinggi yang pernah Anda capai di 365 hari terakhir (filtered 99th percentile)."
                        )

                st.info(
                    "💡 Buka halaman ⚙️ **Settings** dan isi angka di atas, lalu klik "
                    "💾 **Save profile** + 🔄 **Recompute all metrics**. "
                    "Semua zone, TSS, dan IF akan otomatis update jadi lebih akurat."
                )

    st.divider()

    days = st.selectbox("Aggregate window", [7, 14, 28, 90], index=2,
                        format_func=lambda v: f"Last {v} days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = df[df["start_date"] >= cutoff]
    if recent.empty:
        st.info("No runs in this window.")
        return

    # Aggregate zone seconds across all activities in window
    hr_total = {name: 0.0 for name, *_ in config.HR_ZONES}
    pace_total = {name: 0.0 for name, *_ in config.PACE_ZONES}
    for aid in recent["id"]:
        s = db.get_streams(int(aid))
        if not s:
            continue
        h = A.hr_zone_distribution(s, fthr)
        for _, r in h.iterrows():
            hr_total[r["zone"]] += r["seconds"]
        p = A.pace_zone_distribution(s, thr_pace)
        for _, r in p.iterrows():
            pace_total[r["zone"]] += r["seconds"]

    hr_df = pd.DataFrame([{"zone": k, "minutes": v / 60.0} for k, v in hr_total.items()])
    pace_df = pd.DataFrame([{"zone": k, "minutes": v / 60.0} for k, v in pace_total.items()])

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("HR Zones")
        if hr_df["minutes"].sum() < 1:
            st.info("No HR stream data in this window. Check that your watch records HR.")
        else:
            fig = px.bar(hr_df, x="zone", y="minutes",
                         color="zone", text=hr_df["minutes"].map(lambda v: f"{v:.0f} min"))
            fig.update_layout(height=350, showlegend=False, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)
    with col2:
        st.subheader("Pace Zones")
        if pace_df["minutes"].sum() < 1:
            st.info("No pace stream data in this window.")
        else:
            fig = px.bar(pace_df, x="zone", y="minutes",
                         color="zone", text=pace_df["minutes"].map(lambda v: f"{v:.0f} min"))
            fig.update_layout(height=350, showlegend=False, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)

    # 80/20 sanity check
    st.subheader("Polarization check (80/20 rule)")
    pace_pct = pace_df.copy()
    total_min = pace_pct["minutes"].sum() or 1.0
    pace_pct["pct"] = 100 * pace_pct["minutes"] / total_min
    notes = A.weekly_balance_notes(pace_pct.rename(columns={"minutes": "seconds"}))
    for n in notes:
        st.info(n)

    explain(
        "Apa arti tiap zone? (talk test)",
        """
**HR Zones** (% dari Threshold HR Anda):
- **Z1 Recovery** (<85% FTHR) — sangat ringan. Bisa ngobrol panjang lebar. Cocok untuk recovery run, warmup, cooldown.
- **Z2 Aerobic** (85-89%) — easy / conversational. Bisa ngobrol kalimat lengkap. **Ini zona "building base" yang paling penting.** 70-80% waktu lari Anda harusnya di sini.
- **Z3 Tempo** (89-94%) — moderate. Bisa ngobrol pendek (3-5 kata). Zona "no-man's land" — banyak amatir terlalu sering di sini.
- **Z4 Threshold** (94-100%) — comfortably hard. Bisa cuma 1-2 kata. Latihan tempo / cruise interval.
- **Z5 VO₂ Max** (>100%) — hard / max. Cuma bisa napas. Interval pendek.

**Pace Zones** (multiplier dari threshold pace Anda):
- **Z1 Easy** (>1.29× threshold) — recovery / easy
- **Z2 Marathon** (1.14-1.29×) — pace marathon
- **Z3 Tempo** (1.06-1.14×) — pace half-marathon ke 10K
- **Z4 Threshold** (1.00-1.06×) — pace 10K race
- **Z5 VO₂ Max** (0.90-1.00×) — pace 5K race
- **Z6 Anaerobic** (<0.90×) — sprint, lebih cepat dari 5K race pace

⚠️ Kalau **FTHR atau Threshold pace di Settings belum bener**, semua zone analysis ini akan ngaco. Patokan: FTHR ≈ avg HR di 10K race Anda. Threshold pace ≈ pace 10K race Anda.
        """
    )


# ---------------------------------------------------------
# PAGE: Race Predictor
# ---------------------------------------------------------
def page_predictor():
    st.title("Race Predictor")
    st.caption("Riegel formula (T₂ = T₁·(D₂/D₁)^1.06) using your best recent effort.")
    df = load_runs()
    if df.empty:
        st.info("No data yet.")
        return

    st.info(
        "🏁 **Prediksi ini bilang: 'kalau hari ini Anda race 5K/10K/Half/Full, kira-kira "
        "waktu segini'.** Berdasarkan rumus Riegel (1981) yang dipakai luas di running science. "
        "Akurasinya tinggi untuk jarak yang gak terlalu beda jauh dari run referensi. Untuk lompat "
        "5K ke Marathon, prediksi cenderung optimis kalau Anda belum punya basis aerobic yang kuat."
    )

    preds = A.predict_race_times(df)
    if not preds:
        st.warning("Need at least one strong effort ≥ 5 km in the last 90 days.")
        return

    base = preds["5K"]["based_on"]
    st.markdown(
        f"**Based on:** *{base['name']}* on {base['date'].strftime('%Y-%m-%d')} — "
        f"{base['distance_m']/1000:.2f} km in {A.secs_to_hms(base['time_s'])} "
        f"(VDOT ≈ **{base['vdot']:.1f}**)"
    )
    st.caption(
        "💡 Sistem otomatis cari lari **tercepat** Anda (≥5 km, 90 hari terakhir) dan pakai itu "
        "sebagai dasar prediksi. Kalau prediksinya keliatan terlalu optimis, mungkin run itu "
        "dengan downhill/draft/cuaca ideal."
    )

    cols = st.columns(4)
    for col, (race, info) in zip(cols, preds.items()):
        kpi(col, race, A.secs_to_hms(info["time_s"]),
            f"avg {A.pace_to_str(info['pace_min_per_km'])}",
            help_text=f"Estimasi waktu Anda kalau race {race} hari ini, plus pace rata-rata yang harus dipegang.")

    explain(
        "Bagaimana cara baca prediksi ini?",
        """
**Riegel formula:** Time₂ = Time₁ × (Distance₂/Distance₁)^1.06

Eksponen 1.06 itu hasil studi Peter Riegel (1981) yang nge-fit ribuan data race. Intinya: tiap kali Anda double distance, waktu Anda *gak* persis double, tapi double × 1.04 (untuk runners terlatih) sampai × 1.1 (untuk yang base-nya lemah).

**Cara baca prediksi:**
- Lihat selisih antara prediksi 5K vs PR 5K aktual Anda — kalau prediksi jauh lebih cepat dari PR, artinya Anda *bisa* lebih cepat dengan race execution yang bener.
- Prediksi Marathon dari run 5K hampir selalu **terlalu optimis** kalau Anda belum pernah long-run 30+ km. Aerobic endurance ≠ kecepatan murni.

**VDOT** (di samping nama lari referensi) = Daniels VDOT score, semacam "credit score" untuk runner. Naik 1-2 poin per training cycle bagus = progress nyata. Patokan VDOT pria: 40 = casual, 50 = strong amateur, 60 = sub-3 marathoner, 70+ = elit.
        """
    )

    st.divider()
    st.subheader("Goal tracking")
    profile = current_profile()
    goal_race = profile.get("goal_race")
    goal_time_str = profile.get("goal_time")
    if not goal_race or not goal_time_str:
        st.info("Set a target race & time in Settings to track goal progress.")
        return
    # Parse HH:MM:SS or MM:SS
    try:
        parts = [int(p) for p in goal_time_str.split(":")]
        if len(parts) == 2:
            goal_secs = parts[0] * 60 + parts[1]
        else:
            goal_secs = parts[0] * 3600 + parts[1] * 60 + parts[2]
    except Exception:
        st.error(f"Couldn't parse goal time '{goal_time_str}'. Use H:MM:SS or MM:SS.")
        return

    pred_secs = preds.get(goal_race, {}).get("time_s")
    if not pred_secs:
        st.warning(f"No prediction for '{goal_race}' yet.")
        return
    gap = pred_secs - goal_secs
    delta_str = ("on pace" if abs(gap) < 30
                 else f"need {A.secs_to_hms(abs(gap))} faster" if gap > 0
                 else f"{A.secs_to_hms(abs(gap))} ahead of pace")
    c1, c2, c3 = st.columns(3)
    kpi(c1, f"Goal: {goal_race}", goal_time_str)
    kpi(c2, "Current prediction", A.secs_to_hms(pred_secs), delta_str)
    if profile.get("goal_date"):
        try:
            d = datetime.fromisoformat(profile["goal_date"])
            days_left = (d - datetime.now()).days
            kpi(c3, "Days to race", f"{days_left}",
                "Plan a 2–3 week taper if <21d.")
        except Exception:
            pass


# ---------------------------------------------------------
# PAGE: AI Coach
# ---------------------------------------------------------
def page_coach():
    st.title("AI Coach")
    df = load_runs()
    if df.empty:
        st.info("Sync runs first.")
        return

    st.info(
        "🧠 **AI Coach baca data Anda dan kasih saran latihan hari ini.** "
        "Logika-nya seperti pelatih: liat seberapa fresh Anda (TSB), seberapa cepat fitness naik "
        "(ramp rate CTL), lalu rekomendasi sesi yang pas. Saran ini bukan baku — Anda lebih tau "
        "kondisi badan & jadwal, anggap aja ini second opinion data-driven."
    )

    profile = current_profile()
    pmc = A.compute_pmc(A.build_daily_tss(df))
    weekly = A.weekly_summary(df)
    advice = A.coach_recommendation(pmc, weekly, profile)

    st.markdown(
        f"<div class='coach-card {advice.color}'>"
        f"<div style='font-size:1.4rem;font-weight:700'>{advice.headline}</div>"
        f"<div style='margin-top:.4rem;color:#475569'>{advice.rationale}</div>"
        f"<div style='margin-top:.8rem;font-weight:600'>Today's workout:</div>"
        f"<div style='color:#1e293b'>{advice.workout}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Recovery from last activity
    if not df.empty:
        last = df.iloc[0]
        rec_h = A.recovery_hours(last.get("tss"), last.get("intensity_factor"),
                                 last.get("moving_time_min"))
        ready_at = last["start_date"] + pd.Timedelta(hours=rec_h)
        st.subheader("Recovery status")
        c1, c2, c3 = st.columns(3)
        kpi(c1, "Last run", last["name"][:24],
            last["start_date_local"].strftime("%a %d %b %H:%M"),
            help_text="Lari terakhir Anda yang dipakai untuk hitung recovery time.")
        kpi(c2, "Recovery needed", f"{rec_h:.0f} h",
            f"IF {last.get('intensity_factor') or 0:.2f} · TSS {last.get('tss') or 0:.0f}",
            help_text="Estimasi jam sampai badan siap untuk hard workout lagi. Rumus: TSS × 0.5 dikalibrasi dengan intensitas. Easy run = recovery cepat; race effort = sampai 96 jam.")
        # ready_at is tz-aware UTC (start_date is tz-aware), so use tz-aware now
        now_utc = pd.Timestamp.now(tz="UTC")
        remaining = (ready_at - now_utc).total_seconds() / 3600.0
        if remaining <= 0:
            kpi(c3, "Ready for hard session", "✅ Yes",
                f"Fully recovered {-remaining:.0f}h ago",
                help_text="Badan sudah pulih. Hari ini boleh sesi hard (threshold/interval/race).")
        else:
            kpi(c3, "Ready in", f"{remaining:.0f} h",
                f"Around {ready_at.strftime('%a %d %b %H:%M')}",
                help_text="Sebelum waktu ini, sebaiknya cuma easy run / rest / cross-train. Jangan paksa hard workout.")

    explain(
        "Recovery time itu apa, dan kenapa penting?",
        """
**Recovery** = waktu yang badan butuhin untuk *adaptasi* dari latihan sebelumnya. Tanpa recovery cukup:
- Otot gak sempat repair → cedera berulang (shin splints, IT band, plantar fasciitis)
- HRV turun, sleep quality jelek → overtraining syndrome
- Performance plateau atau malah turun

**Rumus yang dipakai:**
- TSS rendah + intensitas easy → recovery cepat (4-12 jam)
- TSS sedang + tempo → recovery sedang (24-36 jam)
- TSS tinggi + threshold/race → recovery lama (48-72 jam)
- Marathon race → bisa 1-2 minggu full recovery

**Tips praktis:**
- Hard day → easy/rest day berikutnya. Hard-hard back-to-back cuma untuk advanced athlete.
- Sleep adalah recovery tool #1. 8+ jam tidur > supplement apapun.
- Easy run sebenarnya *accelerate* recovery (blood flow), lebih bagus dari rest total kalau Anda masih bisa jalan.
        """
    )

    st.divider()
    st.subheader("Weekly summary")
    if not weekly.empty:
        w = weekly.copy()
        w["Week"] = w["week_start"].dt.strftime("%Y-%m-%d")
        w["Distance"] = w["distance_km"].map(lambda v: f"{v:.1f} km")
        w["Time"] = (w["moving_time_min"] * 60).map(A.secs_to_hms)
        w["TSS"] = w["tss"].map(lambda v: f"{v:.0f}")
        w["Avg HR"] = w["avg_hr"].map(lambda v: f"{v:.0f}" if pd.notna(v) else "—")
        w["Avg pace"] = w["avg_pace"].map(A.pace_to_str)
        st.dataframe(w[["Week", "runs", "Distance", "Time", "TSS", "Avg HR", "Avg pace"]],
                     hide_index=True, use_container_width=True)
        st.caption(
            "💡 Cek konsistensi minggu-ke-minggu. **Avg HR yang stabil tapi pace makin cepat** "
            "di easy runs = fitness aerobic naik. **Avg HR naik tapi pace stagnan** = mungkin overtraining."
        )


# ---------------------------------------------------------
# PAGE: Activity Detail
# ---------------------------------------------------------
def _safe_int(x, default=0):
    try:
        if x is None or pd.isna(x):
            return default
        return int(x)
    except Exception:
        return default


def _safe_float(x, default=None):
    try:
        if x is None or pd.isna(x):
            return default
        return float(x)
    except Exception:
        return default


def page_activity():
    st.title("Activity Detail")
    df = load_runs()
    if df.empty:
        st.info("No data yet.")
        return

    st.info(
        "🔍 **Halaman ini = deep analysis 1 lari spesifik.** "
        "Pilih lari di dropdown di bawah, dan Anda bakal liat 12 metric + chart pace/HR + elevation + "
        "per-km splits + best efforts + aerobic durability. Cocok buat post-race debrief atau "
        "ngecek apakah long run akhir pekan kemarin beneran easy seperti yang Anda kira."
    )

    # Sort newest first for the picker
    df = df.sort_values("start_date", ascending=False).reset_index(drop=True)

    # Filter to runs with distance >= 1km to keep the picker clean
    sel_df = df[df["distance_km"] >= 1].reset_index(drop=True)
    if sel_df.empty:
        sel_df = df

    options = [
        f"{r['start_date_local'].strftime('%Y-%m-%d')} — {(r['name'] or 'Untitled')[:50]} "
        f"({r['distance_km']:.2f} km)"
        for _, r in sel_df.iterrows()
    ]
    idx = st.selectbox("Pick a run", range(len(options)), format_func=lambda i: options[i])
    row = sel_df.iloc[idx]
    streams = db.get_streams(int(row["id"]))
    profile = current_profile()

    # ---- KPI row 1: basics
    st.markdown("##### 📏 Basic stats")
    c1, c2, c3, c4 = st.columns(4)
    kpi(c1, "Distance", f"{_safe_float(row['distance_km']) or 0:.2f} km",
        help_text="Total jarak GPS yang tercatat.")
    kpi(c2, "Moving time", A.secs_to_hms(_safe_float(row["moving_time_s"])),
        help_text="Waktu lari aktif (gak termasuk pause/jeda). Beda dengan 'elapsed time' yang termasuk break.")
    kpi(c3, "Avg pace", A.pace_to_str(_safe_float(row["pace_min_per_km"])),
        help_text="Rata-rata pace = moving time ÷ distance. Untuk pace yang lebih akurat per segment, lihat 'Per-km splits' di bawah.")
    avg_hr = _safe_float(row["average_heartrate"])
    kpi(c4, "Avg HR", f"{avg_hr:.0f} bpm" if avg_hr else "—",
        help_text="Rata-rata detak jantung selama lari. Patokan: Avg HR < 70% maxHR = easy run, 80-90% = tempo/threshold, >90% = hard.")

    # ---- KPI row 2: load
    st.markdown("##### 💪 Training load")
    c1, c2, c3, c4 = st.columns(4)
    elev = _safe_float(row["total_elevation_gain_m"])
    kpi(c1, "Elevation gain", f"{elev:.0f} m" if elev is not None else "—",
        help_text="Total naik (gak termasuk turun). Patokan: <100m = flat, 100-300m = rolling, >300m = hilly. Latihan hill ngebantu strength & VO2max.")
    tss = _safe_float(row["tss"])
    kpi(c2, "TSS", f"{tss:.0f}" if tss else "—",
        help_text="Training Stress Score. 100 = 1 jam habis-habisan di threshold. <50 = easy/recovery, 50-100 = moderate, 100-150 = hard workout, 150-250 = race effort, >250 = epic (ultra).")
    intf = _safe_float(row["intensity_factor"])
    kpi(c3, "Intensity Factor", f"{intf:.2f}" if intf else "—",
        help_text="Rata-rata intensitas relatif ke threshold pace Anda. 0.65-0.75 = recovery, 0.75-0.85 = endurance, 0.85-0.95 = tempo, 0.95-1.05 = threshold/10K race, >1.05 = VO2 max / interval.")
    vdot = _safe_float(row["estimated_vo2max"])
    kpi(c4, "VO₂max est.", f"{vdot:.1f}" if vdot else "—",
        help_text="Estimasi VO2 max pakai Daniels formula, kalau lari ini dianggep race-effort. Cuma valid kalau lari ini all-out atau race. Untuk easy run angka ini ngelitik bawah.")

    # ---- KPI row 3: efficiency & recovery
    st.markdown("##### ⚡ Efficiency & body response")
    c1, c2, c3, c4 = st.columns(4)
    ef = A.efficiency_factor(_safe_float(row.get("average_speed_mps")), avg_hr)
    kpi(c1, "Efficiency Factor", f"{ef:.3f}" if ef else "—",
        "Speed (m/min) ÷ HR — higher = better",
        help_text="EF naik dari waktu ke waktu di easy runs yang konsisten = bukti aerobic base Anda makin kuat. Patokan: EF 1.5+ di easy run = aerobic fitness bagus. Jangan dibandingin antar runner, bandingin Anda vs Anda dulu.")
    max_hr = _safe_float(row["max_heartrate"])
    kpi(c2, "Max HR", f"{max_hr:.0f} bpm" if max_hr else "—",
        help_text="Detak jantung maksimum di lari ini. Catat di mana ini terjadi — di awal (panas duluan?), di hill, atau di sprint akhir? Berguna untuk validate Max HR setting Anda.")
    cadence = _safe_float(row["average_cadence"])
    kpi(c3, "Avg cadence", f"{2*cadence:.0f} spm" if cadence else "—",
        "Steps per minute (Strava reports per-foot)",
        help_text="Langkah per menit (kedua kaki). 170-180 spm = sweet spot efficiency. <160 spm = mungkin over-striding (kaki mendarat terlalu jauh di depan badan) yang bikin braking force + cedera knee/shin.")
    rec_h = A.recovery_hours(tss, intf, _safe_float(row["moving_time_min"]))
    kpi(c4, "Recovery needed", f"{rec_h:.0f} h" if rec_h else "—",
        help_text="Estimasi jam sampai badan ready untuk hard session berikutnya. Diturunkan dari TSS + IF. Bukan rumus pasti — listen to body.")

    explain(
        "Cara baca 12 metric di atas",
        """
**3 baris ini ngegambarin 3 dimensi lari Anda:**

🔵 **Basic stats** (baris 1) — fakta dasar: berapa jauh, berapa lama, secepat apa, sekencang apa jantungnya.

🟢 **Training load** (baris 2) — seberapa berat sesi ini. **TSS** angka paling penting buat Anda track:
- < 50 → easy/recovery
- 50-100 → solid endurance run
- 100-150 → hard workout
- 150-250 → race effort
- 250+ → epic / ultra

⚡ **Efficiency & body response** (baris 3) — gimana badan Anda *merespon* effort itu:
- **Efficiency Factor (EF)** — rasio speed/HR. Patokan terbaik: bandingin EF Anda dari easy run bulan lalu vs sekarang. EF naik = aerobic base nguat = "free speed".
- **Cadence** — 170-180 spm ideal. Kalau Anda 150-an, coba metronome app dan naikin perlahan, gak perlu speed naik.
- **Recovery needed** — pakai ini buat plan: kalau hard interval session 60 TSS dengan IF 1.0 = recovery sekitar 48 jam = besok wajib easy/rest.
        """
    )

    if not streams or not streams.get("time"):
        st.warning("No stream data — sync may have skipped detail download for this activity.")
        return

    # ---- Pace & HR over distance with proper formatting
    st.subheader("Pace & HR over distance")
    dist_km = [d / 1000.0 for d in streams.get("distance", [])]
    pace_min = [A.mps_to_pace_min_per_km(v) for v in streams.get("velocity_smooth", [])]
    hr_series = streams.get("heartrate", [None] * len(dist_km))
    pace_strs = [A.pace_to_str(p, suffix="") if p else "—" for p in pace_min]

    fig = go.Figure()
    if any(h is not None for h in hr_series):
        fig.add_scatter(
            x=dist_km, y=hr_series, name="HR (bpm)",
            line=dict(color="#ef4444", width=2),
            hovertemplate="%{x:.2f} km · %{y:.0f} bpm<extra></extra>",
        )
    if any(p is not None for p in pace_min):
        fig.add_scatter(
            x=dist_km, y=pace_min, name="Pace", yaxis="y2",
            line=dict(color="#3b82f6", width=2),
            customdata=pace_strs,
            hovertemplate="%{x:.2f} km · %{customdata}/km<extra></extra>",
        )
        # Build MM:SS tick labels for pace axis
        valid_pace = [p for p in pace_min if p and 2 < p < 12]
        if valid_pace:
            pmin = max(2.5, min(valid_pace) - 0.5)
            pmax = min(12, max(valid_pace) + 0.5)
            tickvals, ticktext = A.pace_axis_ticks(pmin, pmax, 0.5)
            fig.update_layout(yaxis2=dict(
                title="Pace (min:sec/km)", overlaying="y", side="right",
                autorange="reversed", showgrid=False,
                tickvals=tickvals, ticktext=ticktext,
            ))
    fig.update_layout(
        height=380, yaxis=dict(title="HR (bpm)"),
        xaxis=dict(title="Distance (km)"),
        legend=dict(orientation="h", y=1.08),
        margin=dict(t=40, b=10, l=10, r=10),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)

    # ---- Elevation profile
    alt = streams.get("altitude")
    if alt and any(a is not None for a in alt):
        st.subheader("Elevation profile")
        fig = go.Figure()
        fig.add_scatter(x=dist_km, y=alt, fill="tozeroy",
                        line=dict(color="#10b981"), name="Elevation",
                        hovertemplate="%{x:.2f} km · %{y:.0f} m<extra></extra>")
        fig.update_layout(height=220,
                          xaxis=dict(title="Distance (km)"),
                          yaxis=dict(title="Altitude (m)"),
                          margin=dict(t=20, b=10, l=10, r=10))
        st.plotly_chart(fig, use_container_width=True)

    # ---- Per-km splits
    st.subheader("Per-km splits")
    st.caption(
        "💡 Tabel kiri = breakdown waktu, pace, HR, elevation per km. "
        "Chart kanan = pace tiap km (bar lebih rendah = lebih cepat). "
        "**Pola yang dicari:** Positive split (km akhir lebih lambat) = mungkin start kekencengan. "
        "Negative split (km akhir lebih cepat) = pacing bagus. Even split = konsisten."
    )
    splits = A.compute_splits(streams, split_distance_m=1000.0)
    if splits.empty:
        st.info("No split data available.")
    else:
        # Format for display
        view = splits.copy()
        view["Pace"] = view["pace_min_per_km"].map(A.pace_to_str)
        view["Time"] = view["time_s"].map(A.secs_to_hms)
        view["HR"] = view["avg_hr"].map(
            lambda v: f"{v:.0f}" if v and not pd.isna(v) else "—")
        view["Elev +"] = view["elev_gain_m"].map(
            lambda v: f"+{v:.0f} m" if v else "0 m")
        view["Distance"] = view["distance_km"].map(lambda v: f"{v:.2f} km")

        c1, c2 = st.columns([3, 2])
        with c1:
            st.dataframe(
                view[["split", "Distance", "Time", "Pace", "HR", "Elev +"]]
                .rename(columns={"split": "#"}),
                hide_index=True, use_container_width=True, height=380,
            )
        with c2:
            # Bar chart of pace per split (lower bar = faster)
            fig = go.Figure()
            fig.add_bar(
                x=splits["split"], y=splits["pace_min_per_km"],
                marker_color="#3b82f6",
                text=[A.pace_to_str(p, "") for p in splits["pace_min_per_km"]],
                textposition="outside",
                hovertemplate="Split %{x} · %{text}/km<extra></extra>",
            )
            valid = splits["pace_min_per_km"].dropna()
            if not valid.empty:
                pmin = max(2.5, valid.min() - 0.3)
                pmax = min(12, valid.max() + 0.3)
                tickvals, ticktext = A.pace_axis_ticks(pmin, pmax, 0.25)
                fig.update_yaxes(autorange="reversed",
                                 tickvals=tickvals, ticktext=ticktext,
                                 title="Pace")
            fig.update_layout(height=380, xaxis=dict(title="Split #"),
                              margin=dict(t=10, b=10, l=10, r=10),
                              showlegend=False)
            st.plotly_chart(fig, use_container_width=True)

    # ---- Best efforts within this run
    st.subheader("Best efforts (within this run)")
    st.caption(
        "💡 App scan GPS Anda dan cari **sub-segmen tercepat** di lari ini. "
        "Misalnya \"5 km\" = window 5 km tercepat yang Anda lari dalam sesi ini (bisa dari km 3-8, "
        "gak harus dari awal). Berguna untuk lihat 'PR informal' tanpa harus balapan."
    )
    bests = A.best_efforts(streams)
    if not bests:
        st.info("Run too short for best-effort analysis (<400m).")
    else:
        cols = st.columns(len(bests))
        for col, (label, info) in zip(cols, bests.items()):
            kpi(col, label, A.secs_to_hms(info["time_s"]),
                f"@ {A.pace_to_str(info['pace_min_per_km'])}",
                help_text=f"Window {label} tercepat dalam lari ini. Bandingkan dengan PR formal Anda untuk lihat seberapa dekat.")

    # ---- Aerobic decoupling & HR drift
    st.subheader("Aerobic durability")
    st.caption(
        "💡 2 metric ini ngecek apakah aerobic base Anda kuat: "
        "**Decoupling** = apakah pace:HR ratio tetap stabil dari paruh pertama ke paruh kedua. "
        "**HR drift** = seberapa cepat HR naik sepanjang lari. "
        "Cuma akurat di lari long & relatif steady-state (jangan dipakai di interval/race)."
    )
    c1, c2 = st.columns(2)
    with c1:
        dec = A.aerobic_decoupling(streams)
        if dec is None:
            st.info("Need HR + speed streams for decoupling.")
        else:
            color = "green" if dec < 5 else "amber" if dec < 8 else "red"
            interp = ("Excellent aerobic durability — pace stayed efficient relative to HR." if dec < 5
                      else "Moderate decoupling — aerobic base still solid but room to improve." if dec < 8
                      else "High decoupling — HR climbed faster than pace, suggests heat/fatigue/dehydration "
                           "or weak aerobic base. Add more easy long runs.")
            st.markdown(
                f"<div class='coach-card {color}'>"
                f"<div style='font-size:1.2rem;font-weight:700'>Decoupling: {dec:+.1f}%</div>"
                f"<div style='color:#475569;margin-top:.3rem'>{interp}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
    with c2:
        drift = A.hr_drift(streams)
        if drift is None:
            st.info("Need HR stream for drift analysis.")
        else:
            color = "green" if drift < 3 else "amber" if drift < 7 else "red"
            interp = ("Stable HR — strong aerobic condition." if drift < 3
                      else "Mild upward drift — normal on long/hot runs." if drift < 7
                      else "Significant drift — fatigue, heat, or dehydration likely.")
            st.markdown(
                f"<div class='coach-card {color}'>"
                f"<div style='font-size:1.2rem;font-weight:700'>HR drift: {drift:+.1f} bpm/hr</div>"
                f"<div style='color:#475569;margin-top:.3rem'>{interp}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    explain(
        "Aerobic decoupling & HR drift — kenapa penting?",
        """
**Aerobic decoupling (%)**
Bayangin Anda lari 60 menit easy run dengan effort yang sama persis dari awal sampai akhir. Idealnya:
- Pace tetap → sama dari menit ke-5 sampai menit ke-55
- HR tetap → juga sama

Tapi realitanya, HR cenderung naik perlahan meski pace sama (cardiac drift). Decoupling ngitung selisih efisiensi pace:HR antara **paruh pertama** vs **paruh kedua**.

- **< 5%** ✅ Aerobic base SOLID. Anda bisa pertahankan effort lama. Cocok untuk long run / marathon training.
- **5-8%** 🟡 Moderate. Aerobic base oke tapi masih ada ruang.
- **> 8%** ❌ HR naik banyak meski pace gak naik. Bisa karena: aerobic base lemah, dehidrasi, panas, atau pacing awal kekencengan. **Solusi:** tambah easy long runs di Z2 (jangan tergoda nambah intensitas).

---

**HR Drift (bpm/hr)**
Slope kenaikan HR sepanjang lari. Misalnya +4 bpm/hr artinya HR Anda naik 4 bpm tiap 60 menit lari.

- **< 3 bpm/hr** ✅ Sangat stabil — aerobic base & hidrasi top.
- **3-7 bpm/hr** 🟡 Normal untuk long run / kondisi panas / progressive run.
- **> 7 bpm/hr** ❌ Drift besar — kemungkinan dehidrasi, panas, atau over-effort. Pasti coba minum lebih banyak / start lebih easy.

⚠️ **Catatan:** Kedua metric ini cuma akurat di lari **steady-state long** (>30 menit, easy/moderate effort konstan). Jangan dipakai untuk interval, race, atau lari pendek.
        """
    )

    # ---- Zone charts (existing)
    st.subheader("Zone breakdown (this run)")
    st.caption(
        "💡 Berapa menit Anda di tiap intensitas zone. Berguna untuk verify intent: "
        "kalau Anda niat easy run tapi 30% waktu ada di Z4 Threshold, berarti effort-nya kekencengan."
    )
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**HR zones**")
        h = A.hr_zone_distribution(streams, profile.get("fthr") or 170)
        if h.empty or h["seconds"].sum() < 1:
            st.info("No HR data.")
        else:
            h["minutes"] = h["seconds"] / 60.0
            fig = px.bar(h, x="zone", y="minutes", color="zone",
                         text=h["minutes"].map(lambda v: f"{v:.1f} min"))
            fig.update_layout(height=300, showlegend=False,
                              margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)
    with c2:
        st.markdown("**Pace zones**")
        p = A.pace_zone_distribution(streams, profile.get("threshold_pace_min_per_km") or 5.0)
        if p.empty or p["seconds"].sum() < 1:
            st.info("No pace data.")
        else:
            p["minutes"] = p["seconds"] / 60.0
            fig = px.bar(p, x="zone", y="minutes", color="zone",
                         text=p["minutes"].map(lambda v: f"{v:.1f} min"))
            fig.update_layout(height=300, showlegend=False,
                              margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------
# PAGE: Settings
# ---------------------------------------------------------
def _format_pace(p):
    """Helper for displaying min/km pace as MM:SS."""
    if p is None or pd.isna(p) or p <= 0:
        return "—"
    m = int(p)
    s = int(round((p - m) * 60))
    if s == 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}/km"


def _fmt_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------
# PAGE: Training Plan
# ---------------------------------------------------------
def page_training_plan():
    st.title("Training Plan")
    st.caption("Adaptive periodized plan — adjusts to your form & fatigue daily.")

    uid = current_user_id()
    existing = db.get_training_plan(uid)
    today = pd.Timestamp.now().normalize().date()

    # ----- No plan yet -> setup wizard -----
    if not existing:
        st.info(
            "🗓️ **Belum ada training plan aktif.** "
            "Pilih race target Anda di bawah, sistem akan generate plan periodisasi "
            "(Base → Build → Peak → Taper) dengan paces yang dihitung dari goal time Anda."
        )
        _render_setup_wizard(existing=None, today=today)
        return

    # ----- Plan exists -> show dashboard -----
    _render_plan_dashboard(existing, today)


def _render_setup_wizard(existing: dict | None, today):
    """Race-picker form. Generates and saves a new plan on submit."""
    st.subheader("🎯 Setup race & generate plan")

    # Try to pre-fill current weekly mileage and CTL from recent data
    df = load_runs()
    suggested_weekly = 30.0
    suggested_ctl = 40.0
    if not df.empty:
        # Last 4 weeks avg
        df_recent = df.copy()
        df_recent["date_only"] = df_recent["start_date_local"].dt.date
        cutoff = today - timedelta(days=28)
        recent = df_recent[df_recent["date_only"] >= cutoff]
        if not recent.empty:
            total_km = recent["distance_km"].sum()
            suggested_weekly = max(round(total_km / 4.0, 1), 10.0)
        try:
            pmc_df = A.compute_pmc(A.build_daily_tss(df))
            if not pmc_df.empty:
                suggested_ctl = float(pmc_df.iloc[-1]["ctl"])
        except Exception:
            pass

    with st.form("training_plan_setup"):
        c1, c2 = st.columns(2)
        with c1:
            race_type = st.selectbox(
                "Race distance",
                TP.race_options(),
                index=3,  # default Marathon
                help="Pilih jarak race target Anda.",
            )
        with c2:
            min_date = today + timedelta(days=21)   # at least 3 weeks out
            default_date = today + timedelta(weeks=16)
            race_date = st.date_input(
                "Race date",
                value=default_date,
                min_value=min_date,
                max_value=today + timedelta(days=365),
                help="Tanggal balapan Anda. Plan akan dibalik mundur dari tanggal ini.",
            )

        # Show race profile tagline
        prof = TP.RACE_PROFILES[race_type]
        st.caption(f"ℹ️ **{race_type}** — {prof['tagline']}  ·  default {prof['default_weeks']} weeks · peak long run {prof['peak_long_km']:.0f} km")

        # Default target time per race
        default_secs = TP.race_default_target_seconds(race_type)
        c3, c4, c5 = st.columns(3)
        with c3:
            tgt_h = st.number_input("Target time — hours",
                                    min_value=0, max_value=12,
                                    value=default_secs // 3600, step=1)
        with c4:
            tgt_m = st.number_input("min",
                                    min_value=0, max_value=59,
                                    value=(default_secs % 3600) // 60, step=1)
        with c5:
            tgt_s = st.number_input("sec",
                                    min_value=0, max_value=59,
                                    value=default_secs % 60, step=5)
        target_time_s = int(tgt_h * 3600 + tgt_m * 60 + tgt_s)
        if target_time_s > 0:
            tgt_pace = (target_time_s / 60.0) / prof["distance_km"]
            st.caption(f"➡️ Goal pace: **{_format_pace(tgt_pace)}** "
                       f"for {prof['distance_km']:.2f} km")

        st.markdown("---")
        st.markdown("**Volume settings**")
        c6, c7, c8, c9 = st.columns(4)
        with c6:
            runs_per_week = st.slider("Runs / week",
                                       min_value=3, max_value=6, value=5)
        with c7:
            current_weekly = st.number_input(
                "Current weekly km", min_value=0.0, max_value=200.0,
                value=float(round(suggested_weekly, 0)), step=5.0,
                help="Rata-rata mileage Anda 4 minggu terakhir (auto-isi dari data).",
            )
        with c8:
            peak_weekly = st.number_input(
                "Peak weekly km", min_value=10.0, max_value=200.0,
                value=float(prof["default_peak_km"]), step=5.0,
                help="Target volume mingguan tertinggi yang akan Anda capai.",
            )
        with c9:
            # Default plan length = race profile default, but cap by weeks-to-race
            weeks_to_race = max(int((race_date - today).days // 7), 4)
            default_weeks = min(prof["default_weeks"], weeks_to_race)
            plan_weeks = st.number_input(
                "Plan length (weeks)",
                min_value=4, max_value=weeks_to_race,
                value=default_weeks, step=1,
                help=f"Max {weeks_to_race} minggu (dari hari ini ke race date).",
            )

        submitted = st.form_submit_button("🚀 Generate plan", type="primary")

    if submitted:
        if target_time_s <= 0:
            st.error("Target time harus > 0.")
            return
        with st.spinner("Generating periodized plan..."):
            sessions = TP.generate_plan(
                race_type=race_type,
                target_time_s=target_time_s,
                race_date=race_date,
                current_weekly_km=current_weekly,
                peak_weekly_km=peak_weekly,
                runs_per_week=runs_per_week,
                plan_weeks=int(plan_weeks),
                current_fitness_ctl=suggested_ctl,
            )

            plan_start = race_date - timedelta(weeks=int(plan_weeks))
            tgt_pace = (target_time_s / 60.0) / prof["distance_km"]
            meta = {
                "race_type": race_type,
                "race_distance_km": prof["distance_km"],
                "race_date": race_date.isoformat(),
                "target_time_s": int(target_time_s),
                "target_pace_min_per_km": tgt_pace,
                "plan_start_date": plan_start.isoformat(),
                "plan_weeks": int(plan_weeks),
                "runs_per_week": int(runs_per_week),
                "peak_weekly_km": float(peak_weekly),
                "current_weekly_km": float(current_weekly),
                "current_ctl": float(suggested_ctl),
            }
            db.save_training_plan(current_user_id(), meta, [s.to_dict() for s in sessions])
            st.success(f"✅ Plan generated! {len(sessions)} sessions across {plan_weeks} weeks.")
            st.balloons()
            st.rerun()


def _render_plan_dashboard(plan: dict, today):
    """Show the active plan with today's session, week ahead, and full table."""
    race_type = plan["race_type"]
    race_date = pd.to_datetime(plan["race_date"]).date()
    days_to_race = (race_date - today).days
    target_pace = plan.get("target_pace_min_per_km")
    target_time_s = int(plan["target_time_s"])

    # Header KPIs
    c1, c2, c3, c4 = st.columns(4)
    kpi(c1, "Race", f"{race_type}",
        help_text="Jarak race target Anda.")
    kpi(c2, "Race date",
        race_date.strftime("%a %d %b %Y"),
        f"{days_to_race} hari lagi" if days_to_race >= 0 else "PASSED",
        help_text="Hari balapan. Plan dibangun mundur dari tanggal ini.")
    kpi(c3, "Goal time", _fmt_time(target_time_s),
        f"@ {_format_pace(target_pace)}",
        help_text="Target finish time + goal pace yang dipakai turunin semua training paces.")
    kpi(c4, "Plan length",
        f"{plan['plan_weeks']} weeks · {plan['runs_per_week']} runs/wk",
        f"Peak {plan.get('peak_weekly_km', 0):.0f} km/wk",
        help_text="Panjang plan dan target volume puncak per minggu.")

    st.divider()

    # ----- Today's session card -----
    st.subheader("📍 Today's session")
    today_iso = today.isoformat()
    today_row = db.get_planned_session_on(current_user_id(), today)
    if not today_row:
        st.info("Tidak ada sesi terjadwal hari ini (plan mungkin belum mulai atau sudah selesai).")
    else:
        # Build PlannedSession to feed the adjustment engine
        planned = TP.PlannedSession(
            date=today,
            week_num=today_row["week_num"],
            phase=today_row["phase"],
            session_type=today_row["session_type"],
            distance_km=today_row["distance_km"] or 0,
            duration_min=today_row["duration_min"] or 0,
            target_pace_min_per_km=today_row["target_pace_min_per_km"],
            target_hr_zone=today_row["target_hr_zone"] or "",
            description=today_row["description"] or "",
            workout_detail=today_row["workout_detail"] or "",
            is_quality=bool(today_row["is_quality"]),
        )

        # Pull current TSB & ramp from PMC
        current_tsb = None
        ramp_rate = None
        days_since_quality = None
        try:
            df_runs = load_runs()
            if not df_runs.empty:
                pmc_df = A.compute_pmc(A.build_daily_tss(df_runs))
                if not pmc_df.empty:
                    last_row = pmc_df.iloc[-1]
                    current_tsb = float(last_row["tsb"])
                    if len(pmc_df) >= 8:
                        ramp_rate = float(last_row["ctl"] - pmc_df.iloc[-8]["ctl"])
                # Days since last quality session in planned history
                df_planned_past = db.get_planned_sessions_df(current_user_id(),
                    date_to=(today - timedelta(days=1)).isoformat()
                )
                if not df_planned_past.empty:
                    quals = df_planned_past[df_planned_past["is_quality"]]
                    if not quals.empty:
                        last_q = quals.iloc[-1]["date"]
                        days_since_quality = (today - last_q).days
        except Exception:
            pass

        adj = TP.adjust_session_for_today(
            planned=planned,
            current_tsb=current_tsb,
            ramp_rate_7d=ramp_rate,
            days_since_quality=days_since_quality,
        )

        emoji = TP.SESSION_TYPES.get(planned.session_type, "🏃")
        badge_color = {"none": "✅", "minor": "🟡", "major": "🛑"}.get(adj.severity, "✅")
        st.markdown(
            f"### {emoji} **Week {planned.week_num} · {planned.phase} phase · "
            f"{planned.session_type}**"
        )

        cc1, cc2, cc3, cc4 = st.columns(4)
        kpi(cc1, "Distance", f"{planned.distance_km:.1f} km",
            help_text="Total jarak target untuk sesi ini.")
        kpi(cc2, "Duration", f"~{planned.duration_min:.0f} min",
            help_text="Estimasi durasi berdasarkan target pace.")
        kpi(cc3, "Target pace",
            _format_pace(planned.target_pace_min_per_km),
            help_text="Pace target. Untuk interval, ini pace average kerja.")
        kpi(cc4, "HR zone", planned.target_hr_zone or "—",
            help_text="Zone heart rate target.")

        st.markdown(f"**Workout:** {planned.workout_detail}")

        st.markdown(f"#### {badge_color} Adaptive recommendation")
        if adj.severity == "none":
            st.success(f"**{adj.adjusted_description}**\n\n_{adj.reason}_")
        elif adj.severity == "minor":
            st.warning(f"**{adj.adjusted_description}**\n\n_{adj.reason}_")
        else:
            st.error(f"**{adj.adjusted_description}**\n\n_{adj.reason}_")

        explain(
            "Kenapa rekomendasinya bisa beda dari plan asli?",
            """
Adaptive engine cek 4 hal sebelum nyaranin sesi:

1. **Critical overload** — Kalau TSB Anda < −30 atau CTL ramp > +8/minggu → swap ke recovery (resiko cedera tinggi).
2. **Elevated morning HR** — Kalau resting HR pagi >10% di atas baseline + sesi quality dijadwalkan → ganti easy run.
3. **Moderate fatigue** — Kalau TSB < −20 di hari quality → potong volume 30%.
4. **Bonus opportunity** — Kalau TSB > +15 di hari easy dan udah 3+ hari ga ada quality → optional tempo bonus.

Plan ini cuma cetak biru. Yang bener pelatih kasih ke atlet itu fleksibilitas — angka itu panduan, badan Anda yang putuskan akhirnya.
            """,
        )

    st.divider()

    # ----- This week's schedule -----
    st.subheader("🗓️ This week")
    # Find the Monday of the current week
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    df_week = db.get_planned_sessions_df(current_user_id(),
        date_from=week_start.isoformat(),
        date_to=week_end.isoformat(),
    )
    if df_week.empty:
        st.info("Tidak ada sesi minggu ini.")
    else:
        # Build a calendar-style display
        cols = st.columns(7)
        for i, day_offset in enumerate(range(7)):
            d = week_start + timedelta(days=day_offset)
            with cols[i]:
                day_name = d.strftime("%a")
                day_num = d.strftime("%d/%m")
                is_today = d == today
                header = f"**{day_name} {day_num}**"
                if is_today:
                    header = f"📍 {header}"
                st.markdown(header)

                sess = df_week[df_week["date"] == d]
                if sess.empty:
                    st.caption("—")
                    continue
                row = sess.iloc[0]
                emoji = TP.SESSION_TYPES.get(row["session_type"], "🏃")
                if row["session_type"] == "Rest":
                    st.caption(f"{emoji} Rest")
                else:
                    st.caption(
                        f"{emoji} **{row['session_type']}**  \n"
                        f"{row['distance_km']:.1f} km  \n"
                        f"_{_format_pace(row['target_pace_min_per_km'])}_"
                    )

    st.divider()

    # ----- Weekly volume progression chart -----
    st.subheader("📊 Volume progression")
    df_all = db.get_planned_sessions_df(current_user_id())
    if not df_all.empty:
        weekly = df_all.groupby("week_num").agg(
            km=("distance_km", "sum"),
            quality=("is_quality", "sum"),
            phase=("phase", "first"),
        ).reset_index()

        phase_color = {
            "Base":  "#94a3b8",
            "Build": "#3b82f6",
            "Peak":  "#f59e0b",
            "Taper": "#10b981",
            "Race":  "#ef4444",
        }
        fig = go.Figure()
        fig.add_bar(
            x=weekly["week_num"], y=weekly["km"],
            marker_color=[phase_color.get(p, "#888") for p in weekly["phase"]],
            text=[f"{p}" for p in weekly["phase"]],
            textposition="outside",
            name="Weekly km",
        )
        fig.update_layout(
            height=320,
            xaxis_title="Week",
            yaxis_title="Total km",
            showlegend=False,
            margin=dict(t=20, b=10, l=10, r=10),
        )
        st.plotly_chart(fig, use_container_width=True)

    # ----- Full plan table (expandable) -----
    with st.expander("📋 Full plan — all sessions"):
        df_show = df_all.copy()
        df_show["pace"] = df_show["target_pace_min_per_km"].apply(_format_pace)
        df_show["date"] = pd.to_datetime(df_show["date"]).dt.strftime("%a %d %b")
        df_show = df_show[[
            "date", "week_num", "phase", "session_type",
            "distance_km", "duration_min", "pace", "target_hr_zone",
            "description",
        ]].rename(columns={
            "week_num": "wk",
            "session_type": "type",
            "distance_km": "km",
            "duration_min": "min",
            "target_hr_zone": "zone",
        })
        st.dataframe(df_show, use_container_width=True, hide_index=True)

    st.divider()

    # ----- Plan management -----
    st.subheader("⚙️ Plan management")
    cc1, cc2 = st.columns(2)
    with cc1:
        if st.button("♻️ Edit / regenerate plan", use_container_width=True):
            st.session_state["edit_plan"] = True
    with cc2:
        if st.button("🗑️ Delete plan", use_container_width=True, type="secondary"):
            db.delete_training_plan(current_user_id())
            st.success("Plan deleted.")
            st.rerun()

    if st.session_state.get("edit_plan"):
        st.divider()
        st.warning("⚠️ Generating a new plan akan replace plan lama.")
        _render_setup_wizard(existing=plan, today=today)


def page_settings():
    st.title("Settings")
    profile = current_profile()

    st.subheader("Athlete profile")
    with st.form("profile"):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Name", profile.get("name") or "")
            age = st.number_input("Age", 10, 100, int(profile.get("age") or 30))
            resting = st.number_input("Resting HR", 30, 100, int(profile.get("resting_hr") or 60))
            max_hr = st.number_input("Max HR", 120, 230, int(profile.get("max_hr") or 190))
        with c2:
            fthr = st.number_input("Functional Threshold HR (FTHR / LTHR)", 100, 220,
                                   int(profile.get("fthr") or 170),
                                   help="HR you can hold for ~1 hour all-out. ≈ avg HR of a 10K race.")
            thr_pace = st.number_input("Threshold pace (min/km)", 2.5, 9.0,
                                       float(profile.get("threshold_pace_min_per_km") or 5.0),
                                       step=0.05, format="%.2f",
                                       help="Pace you can hold for ~1 hour all-out. ≈ 10K race pace.")
            weight = st.number_input("Weight (kg, optional)", 30.0, 200.0,
                                     float(profile.get("weight_kg") or 65.0))

        st.subheader("Goal race (optional)")
        c3, c4, c5 = st.columns(3)
        with c3:
            goal_race = st.selectbox("Race", ["", "5K", "10K", "Half Marathon", "Marathon"],
                                     index=(["", "5K", "10K", "Half Marathon", "Marathon"]
                                            .index(profile.get("goal_race") or "")))
        with c4:
            goal_time = st.text_input("Goal time (H:MM:SS or MM:SS)",
                                      profile.get("goal_time") or "")
        with c5:
            goal_date = st.text_input("Goal date (YYYY-MM-DD)",
                                      profile.get("goal_date") or "")

        if st.form_submit_button("💾 Save profile", type="primary"):
            db.save_profile(current_user_id(), {
                "name": name, "age": age, "resting_hr": resting, "max_hr": max_hr,
                "fthr": fthr, "threshold_pace_min_per_km": thr_pace,
                "weight_kg": weight, "goal_race": goal_race or None,
                "goal_time": goal_time or None, "goal_date": goal_date or None,
            })
            clear_caches()
            st.success("Saved.")

    st.divider()
    st.subheader("🎯 Auto-detect threshold from your data")
    st.markdown(
        "Daripada nebak FTHR & threshold pace manual, biarkan sistem **scan data lari Anda** "
        "dan hitung otomatis. Cara kerjanya: sistem cari window 30-menit tercepat yang pernah "
        "Anda lari → pace + HR di situ ≈ threshold pace + FTHR asli Anda (metode standar "
        "sport science Coggan/Daniels)."
    )

    df_runs = load_runs()
    c1, c2 = st.columns([1, 3])
    with c1:
        do_detect = st.button("🔍 Scan my data", type="primary",
                              disabled=df_runs.empty, key="settings_auto_detect")
    with c2:
        if df_runs.empty:
            st.caption("Sync activities first before scanning.")
        else:
            st.caption(f"Will scan {len(df_runs)} runs from the last 180 days. Takes ~30 sec.")

    if do_detect:
        with st.spinner("Scanning sustained 30-min efforts across all your runs..."):
            result = A.auto_detect_thresholds(
                df_runs, db.get_streams, lookback_days=180, target_duration_s=1800
            )
            max_hr_est = A.estimate_max_hr_from_data(df_runs)

        if not result:
            st.warning(
                "Tidak ada lari ≥30 menit dengan stream HR/GPS lengkap dalam 180 hari terakhir. "
                "Pastikan jam tangan Anda merekam HR + GPS, atau coba sync ulang aktivitas."
            )
        else:
            st.success(
                f"✅ Scanned **{result['candidates_count']}** sustained efforts. "
                f"Best 30-min effort dari *{result['based_on_activity_name']}* "
                f"({result['based_on_date'].strftime('%d %b %Y')})."
            )
            cc1, cc2, cc3 = st.columns(3)
            new_pace = result['threshold_pace_min_per_km']
            new_fthr = result.get('fthr')
            with cc1:
                delta_pace = new_pace - thr_pace
                arrow = ("🔻 lebih cepat" if delta_pace < -0.05
                         else "🔺 lebih lambat" if delta_pace > 0.05 else "≈ sama")
                st.metric("Suggested Threshold pace", A.pace_to_str(new_pace),
                          f"Setting Anda: {A.pace_to_str(thr_pace)} ({arrow})",
                          delta_color="off")
            with cc2:
                if new_fthr:
                    delta_f = new_fthr - fthr
                    arrow = ("🔺 lebih tinggi" if delta_f > 2
                             else "🔻 lebih rendah" if delta_f < -2 else "≈ sama")
                    st.metric("Suggested FTHR", f"{new_fthr} bpm",
                              f"Setting Anda: {fthr} bpm ({arrow})",
                              delta_color="off")
                else:
                    st.metric("Suggested FTHR", "—", "No HR in best effort")
            with cc3:
                if max_hr_est:
                    st.metric("Detected Max HR", f"{max_hr_est} bpm",
                              f"Setting Anda: {max_hr} bpm",
                              delta_color="off")

            # Apply button
            if st.button("✅ Terapkan ke profil saya", type="primary", key="apply_thresholds"):
                updates = {
                    "name": name, "age": age, "resting_hr": resting,
                    "max_hr": max_hr_est if max_hr_est else max_hr,
                    "fthr": new_fthr if new_fthr else fthr,
                    "threshold_pace_min_per_km": new_pace,
                    "weight_kg": weight,
                    "goal_race": profile.get("goal_race"),
                    "goal_time": profile.get("goal_time"),
                    "goal_date": profile.get("goal_date"),
                }
                db.save_profile(current_user_id(), updates)
                clear_caches()
                st.success(
                    "✅ Profil di-update. Semua zone analysis, TSS, dan IF akan recompute "
                    "saat Anda navigate ke halaman lain. Refresh page untuk lihat nilai baru di form di atas."
                )
                st.balloons()

    st.divider()
    st.subheader("How to set your zones manually")
    st.markdown("""
- **Max HR** — best from a max-effort uphill 4×4 min, or 220 − age as a rough fallback.
- **Functional Threshold HR (FTHR)** — average HR of a 30-min all-out time trial (use last 20 min).
  Roughly equals your 10K race HR.
- **Threshold pace** — your 10K race pace, or 15K race pace if you race longer.

These values drive TSS, IF, and all your zone analyses. Update them after any fitness test or PR.
    """)

    st.divider()
    st.subheader("Data")
    n = db.count_activities(current_user_id())
    st.write(f"**{n}** activities stored locally in `data/runcoach.db`")
    if st.button("Recompute all metrics"):
        clear_caches()
        st.success("Cleared cache — metrics will recompute on next page load.")


# ---------------------------------------------------------
# Router
# ---------------------------------------------------------
ROUTES = {
    "🏠 Overview":            page_overview,
    "📈 Training Load (PMC)": page_pmc,
    "🎯 Zone Analysis":       page_zones,
    "🏁 Race Predictor":      page_predictor,
    "🧠 AI Coach":            page_coach,
    "🗓️ Training Plan":       page_training_plan,
    "🔍 Activity Detail":     page_activity,
    "⚙️ Settings":            page_settings,
}
ROUTES[page]()
