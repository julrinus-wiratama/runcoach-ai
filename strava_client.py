"""
Strava API client: OAuth (Authorization Code), token refresh,
paginated activity fetch, and per-activity streams.

Multi-user: setiap fungsi yang menyentuh data per-user butuh user_id
(yang sama dengan Strava athlete_id).

Docs:
- OAuth:      https://developers.strava.com/docs/authentication/
- Activities: https://developers.strava.com/docs/reference/#api-Activities
- Streams:    https://developers.strava.com/docs/reference/#api-Streams
"""
import time
from typing import List, Dict, Any, Optional
import requests

import config
import database as db


class StravaError(Exception):
    pass


# ---------- OAuth ----------
def build_authorize_url() -> str:
    return (
        f"{config.STRAVA_AUTH_URL}"
        f"?client_id={config.STRAVA_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={config.STRAVA_REDIRECT_URI}"
        f"&approval_prompt=auto"
        f"&scope={config.STRAVA_SCOPES}"
    )


def exchange_code_for_token(code: str) -> dict:
    """
    Exchange the OAuth `code` (from redirect URL) for tokens.
    Saves tokens to DB scoped to the athlete's id, dan return data
    termasuk `athlete_id` supaya caller bisa simpan ke session_state.
    """
    r = requests.post(
        config.STRAVA_TOKEN_URL,
        data={
            "client_id": config.STRAVA_CLIENT_ID,
            "client_secret": config.STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise StravaError(f"Token exchange failed: {r.status_code} {r.text}")
    data = r.json()
    athlete = data.get("athlete") or {}
    user_id = athlete.get("id")
    if not user_id:
        raise StravaError("Strava response missing athlete id")

    db.upsert_user(user_id, athlete)
    db.save_tokens(
        user_id,
        data["access_token"],
        data["refresh_token"],
        data["expires_at"],
        athlete,
    )
    # Untuk convenience caller
    data["athlete_id"] = user_id
    return data


def refresh_access_token(user_id: int) -> str:
    """Refresh access token if expired; returns a valid access token."""
    tok = db.get_tokens(user_id)
    if not tok:
        raise StravaError("Not authenticated. Connect to Strava first.")
    if tok["expires_at"] - 60 > time.time():
        return tok["access_token"]
    r = requests.post(
        config.STRAVA_TOKEN_URL,
        data={
            "client_id": config.STRAVA_CLIENT_ID,
            "client_secret": config.STRAVA_CLIENT_SECRET,
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise StravaError(f"Token refresh failed: {r.status_code} {r.text}")
    data = r.json()
    db.save_tokens(user_id, data["access_token"], data["refresh_token"], data["expires_at"])
    return data["access_token"]


def _auth_headers(user_id: int) -> dict:
    return {"Authorization": f"Bearer {refresh_access_token(user_id)}"}


# ---------- API calls ----------
def _get(user_id: int, path: str, params: Optional[dict] = None) -> Any:
    url = f"{config.STRAVA_API_BASE}{path}"
    r = requests.get(url, headers=_auth_headers(user_id), params=params or {}, timeout=30)
    if r.status_code == 429:
        raise StravaError("Strava rate limit hit. Wait 15 minutes and try again.")
    if r.status_code != 200:
        raise StravaError(f"GET {path} failed: {r.status_code} {r.text}")
    return r.json()


def fetch_athlete(user_id: int) -> dict:
    return _get(user_id, "/athlete")


def fetch_activities_page(user_id: int, after_ts: Optional[int] = None,
                          page: int = 1, per_page: int = 100) -> List[dict]:
    params = {"page": page, "per_page": per_page}
    if after_ts:
        params["after"] = after_ts
    return _get(user_id, "/athlete/activities", params=params)


def fetch_activity_detail(user_id: int, activity_id: int) -> dict:
    return _get(user_id, f"/activities/{activity_id}", params={"include_all_efforts": "false"})


def fetch_streams(user_id: int, activity_id: int) -> Dict[str, list]:
    """Fetch streams (time series) for an activity."""
    keys = "time,distance,heartrate,velocity_smooth,cadence,altitude"
    raw = _get(user_id, f"/activities/{activity_id}/streams",
               params={"keys": keys, "key_by_type": "true"})
    out: Dict[str, list] = {}
    for k, v in raw.items():
        out[k] = v.get("data", [])
    return out


# ---------- High-level sync ----------
def sync_activities(user_id: int, progress_cb=None,
                    max_pages: int = 50,
                    fetch_streams_for_new: bool = True) -> int:
    """
    Pull new activities for `user_id` since the latest stored one.
    Returns count of new activities.
    """
    from datetime import datetime, timezone
    last = db.get_latest_activity_date(user_id)
    after_ts = None
    if last:
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        after_ts = int(dt.replace(tzinfo=timezone.utc).timestamp())

    new_count = 0
    for page in range(1, max_pages + 1):
        batch = fetch_activities_page(user_id, after_ts=after_ts, page=page, per_page=100)
        if not batch:
            break
        for act in batch:
            db.upsert_activity(user_id, act)
            new_count += 1
            if fetch_streams_for_new and act.get("id") and not db.has_streams(act["id"]):
                try:
                    streams = fetch_streams(user_id, act["id"])
                    db.save_streams(act["id"], streams)
                except StravaError:
                    pass
            if progress_cb:
                progress_cb(new_count, act.get("name", ""))
        if len(batch) < 100:
            break
    return new_count
