# RunCoach AI — Deployment Guide

Panduan deploy app ke **Streamlit Community Cloud** (gratis) supaya kamu
dan pacar bisa pake bareng dari mana aja, tanpa harus selalu nyalain laptop.

Total waktu: **~30 menit** dari awal sampai live.

---

## ⚠️ Penting: rotate Strava client secret dulu

File `.streamlit/secrets.toml.example` lama mengandung **real Strava
client secret** (yang sekarang sudah saya sanitize). Sebelum push code ke
GitHub, **rotate secret-nya dulu** biar aman:

1. Buka <https://www.strava.com/settings/api>
2. Di section **My API Application**, klik **Update**
3. Scroll ke bawah, klik **"Reset client secret"** atau **"Regenerate"**
4. Copy client secret yang baru — kamu butuh ini di step 5

> Kalau kamu yakin secret yang lama belum pernah ke-push ke GitHub public,
> step ini opsional tapi tetap recommended.

---

## Step 1 — Siapkan code di lokal

Pastikan ini semua sudah benar (saya sudah set up untuk kamu):

- ✅ `.gitignore` blok `.env`, `data/`, `.streamlit/secrets.toml`
- ✅ `.streamlit/config.toml` — theme & server config
- ✅ `.streamlit/secrets.toml.example` — template tanpa real secret
- ✅ `requirements.txt` — semua dependency
- ✅ `config.py` — udah support `st.secrets` (Streamlit Cloud) + `.env` (lokal)

Cek sekali:

```bash
cd "/Users/julrinuswiratama/Documents/JW/STRAVA ANALIST"
cat .gitignore        # pastikan .env dan data/ ada
ls .streamlit/        # pastikan ada config.toml + secrets.toml.example
```

---

## Step 2 — Buat GitHub repo

Streamlit Community Cloud butuh GitHub repo (public, kalau pakai free tier).

### 2a. Install Git (kalau belum ada)

```bash
git --version    # kalau error, install dulu: brew install git
```

### 2b. Init repo & first commit

```bash
cd "/Users/julrinuswiratama/Documents/JW/STRAVA ANALIST"

# Init git
git init
git branch -M main

# Cek dulu file apa aja yang BAKAL ke-commit (jangan sampai .env ikut)
git status

# Pastikan .env, data/, .venv/ TIDAK ada di list "to be committed".
# Kalau muncul, .gitignore-nya belum efektif. Cek lagi.

# Add semua file yang aman
git add .
git status   # double check — JANGAN ada .env atau secrets.toml asli di staging

git commit -m "Initial commit — RunCoach AI multi-user"
```

### 2c. Buat repo di GitHub

1. Buka <https://github.com/new>
2. Repo name: `runcoach-ai` (atau apapun)
3. **Public** (wajib untuk Streamlit Cloud free tier)
4. **Jangan** centang "Add a README" atau ".gitignore" (kita sudah punya)
5. Klik **Create repository**

GitHub akan kasih command untuk push. Copy yang bagian "**…or push an
existing repository from the command line**", kira-kira:

```bash
git remote add origin https://github.com/<USERNAME>/runcoach-ai.git
git push -u origin main
```

GitHub akan minta auth — kalau pertama kali, install
[GitHub CLI](https://cli.github.com/) atau pakai
[Personal Access Token](https://github.com/settings/tokens).

### 2d. Verifikasi tidak ada secret yang ke-leak

Buka repo di GitHub.com, browse file-nya. **Pastikan:**

- ❌ `.env` **TIDAK ADA** di repo
- ❌ `.streamlit/secrets.toml` **TIDAK ADA** (cuma `.example` yang ada)
- ❌ `data/runcoach.db` **TIDAK ADA**

Kalau ada salah satu, **stop sekarang** dan kasih tau saya — kita perlu
clean history dulu sebelum lanjut.

---

## Step 3 — Deploy ke Streamlit Community Cloud

1. Buka <https://share.streamlit.io/> dan **Sign in with GitHub**
2. Klik **New app** (pojok kanan atas)
3. Isi form:
   - **Repository**: `<USERNAME>/runcoach-ai`
   - **Branch**: `main`
   - **Main file path**: `app.py`
   - **App URL**: pilih subdomain, contoh `runcoach-ai-jul`
     (URL final akan jadi `https://runcoach-ai-jul.streamlit.app`)
4. **Klik "Advanced settings"** sebelum deploy:
   - **Python version**: 3.11 (atau biarin default)
5. Klik **Deploy!**

Build pertama biasanya 2-5 menit. App akan crash pertama kali karena belum
ada secrets — itu normal, kita set di step berikutnya.

---

## Step 4 — Set secrets di Streamlit Cloud

1. Di Streamlit Cloud dashboard, klik app kamu
2. Klik **⚙ Settings** (atau tiga titik → **Settings**)
3. Tab **Secrets**
4. Paste template ini (ganti value sesuai punya kamu):

```toml
STRAVA_CLIENT_ID = "248209"
STRAVA_CLIENT_SECRET = "your_NEW_client_secret_here"
STRAVA_REDIRECT_URI = "https://runcoach-ai-jul.streamlit.app"

ATHLETE_NAME = "Runner"
ATHLETE_AGE = "30"
ATHLETE_RESTING_HR = "60"
ATHLETE_MAX_HR = "190"
ATHLETE_FTHR = "170"
ATHLETE_THRESHOLD_PACE = "5.00"
```

5. **Penting**: ganti `STRAVA_REDIRECT_URI` ke URL Streamlit Cloud app
   kamu (yang dapat dari step 3). Trailing slash JANGAN ada.
6. Klik **Save**
7. App akan auto-rerun, tunggu sampai status hijau "Running"

---

## Step 5 — Update Strava redirect URI

Strava harus tau bahwa redirect ke Streamlit Cloud URL itu legit.

1. Buka <https://www.strava.com/settings/api>
2. Di **Authorization Callback Domain**, isi:
   ```
   runcoach-ai-jul.streamlit.app
   ```
   (cuma domain, **TANPA** `https://` dan tanpa path)
3. Klik **Update**

> Catatan: kalau mau tetap bisa development lokal di `localhost:8501`,
> tambahkan **2 domain** di Strava settings — pisahkan dengan koma.
> Tapi Strava cuma support 1 callback domain di free dev account, jadi
> kemungkinan kamu harus pilih satu. Lokal lebih sering = jangan ganti.
> Cloud lebih sering = ganti ke streamlit.app.

---

## Step 6 — Test & share

1. Buka URL Streamlit Cloud kamu di browser
2. Klik **🚴 Connect with Strava**
3. Authorize → kamu akan redirect balik dan muncul `?u=<athlete_id>` di URL
4. Sidebar muncul **Connected: <nama kamu>**
5. Klik **🔄 Sync activities** → 507 activities lama akan auto-claim (karena
   ada placeholder data dari single-user lama)
6. Klik-klik semua page (Overview, PMC, Zone Analysis, dll) untuk
   memastikan datanya bener

### Share ke pacar

Kasih dia 2 hal:

1. **URL app**: `https://runcoach-ai-jul.streamlit.app`
2. **Instruksi singkat**:
   > Buka URL → klik **"Connect with Strava"** → login pakai akun Strava
   > kamu → authorize. Setelah itu data kamu sendiri yang muncul, terpisah
   > 100% dari aku.

Setiap user yang connect dengan akun Strava beda otomatis dapet data
sendiri. Sidebar bakal nunjukin "Continue as <nama>" untuk balik ke akun
yang udah pernah connect.

---

## Step 7 — Limitations & gotchas

### 7a. Database persistence di free tier

Streamlit Community Cloud free tier punya **ephemeral filesystem**. Artinya:

- ✅ Selama container hidup (biasanya berhari-hari/minggu), data aman
- ⚠️ Kalau app sleep terlalu lama atau di-redeploy, **SQLite di-wipe**
- ✅ Activities Strava bisa di-resync (data ada di Strava)
- ❌ **Training plan kamu HILANG** kalau container restart

**Mitigation**: kalau plan kamu penting, export ke spreadsheet manual
(via expander "Lihat full plan" di Training Plan page) sebelum re-deploy.

Untuk persistensi 100%, butuh upgrade ke database eksternal (Supabase
Postgres ~free 500MB, atau Turso libsql). Bilang aja kalau mau saya
migrate ke sana.

### 7b. URL = "password" kamu

Kamu pilih opsi tanpa login formal. Artinya:

- Siapa pun yang punya URL `runcoach-ai-jul.streamlit.app` bisa buka app
- Tapi mereka **tidak bisa lihat data kamu** kecuali punya akses ke akun
  Strava kamu
- Risk: mereka bisa connect akun Strava mereka sendiri pakai infra kamu
  (gak bahaya, tapi bikin daftar user di landing makin panjang)

Kalau mulai ada user random connect, kamu bisa:
- Tambah password gate (uncomment `APP_PASSWORD` lagi di config)
- Atau ganti URL Streamlit Cloud-nya

### 7c. Strava API rate limit

Free Strava API: **100 requests / 15 menit**, **1000 / hari**.

Sync 507 activities sekali jalan = ~507 GET (1 untuk list, 506 untuk
streams). Ini akan hit rate limit. Kalau gitu, tunggu 15 menit terus
sync lagi — pagination resumable.

Untuk 2-5 user, rate limit shared per app, jadi koordinasi sync timing.

---

## Step 8 — Update code setelah deploy

Setiap kali kamu push commit baru ke `main`, Streamlit Cloud auto-deploy.

```bash
cd "/Users/julrinuswiratama/Documents/JW/STRAVA ANALIST"
# Edit code...
git add .
git commit -m "Add feature X"
git push
# Tunggu 1-2 menit, Streamlit Cloud auto-rebuild
```

---

## Troubleshooting

### "Strava API keys belum di-set"
→ Step 4 belum dilakukan, atau secret-nya typo. Cek lagi.

### OAuth: "redirect_uri mismatch"
→ `STRAVA_REDIRECT_URI` di Streamlit secrets BEDA dengan Authorization
  Callback Domain di Strava settings. Pastikan domain sama.

### App stuck di "Please wait..."
→ Streamlit Cloud lagi build. Lihat log di **Manage app → Logs**.

### Pacar gak bisa connect — bilang akun Strava bermasalah
→ Strava OAuth scope-nya `activity:read_all` — dia harus authorize itu.
  Cek setting privacy Strava-nya (kalau Enhanced Privacy aktif, beberapa
  data gak bisa dipull).

### Data hilang setelah beberapa hari
→ Streamlit Cloud container restart, SQLite ephemeral di-wipe. Re-sync
  dari Strava lagi. Kalau training plan hilang, generate ulang.

---

## Cheat sheet command

```bash
# Local dev
cd "/Users/julrinuswiratama/Documents/JW/STRAVA ANALIST"
./run.sh                               # jalanin di localhost:8501

# Push update
git add . && git commit -m "msg" && git push

# Lihat status git
git status                             # apa yang belum di-commit
git log --oneline -5                   # 5 commit terakhir
```
