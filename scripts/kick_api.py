"""Kick.com API client (Cloudflare-aware, curl_cffi Chrome impersonation).

2026-07-31 実測の現行API:
  - kick.com/api/v2/channels/{slug}            -> channel info (id, livestream)
  - kick.com/api/v2/channels/{slug}/videos     -> VOD list (duration ms, start_time, video.uuid)
  - kick.com/api/v2/channels/{cid}/messages    -> chat replay (start_time= ISO, ~5s window)
  - web.kick.com/api/v1/chat/{cid}/history     -> chat replay fallback (25件)
  - web.kick.com/api/v1/channels/{cid}/videos/{uuid} -> video meta fallback
"""
import json
import sys
import time

from curl_cffi import requests as cffi_requests

IMPERSONATE = "chrome"
BASE = "https://kick.com"
WEB_BASE = "https://web.kick.com"

_session = None


def session():
    global _session
    if _session is None:
        _session = cffi_requests.Session(impersonate=IMPERSONATE)
        _session.headers.update({"Accept": "application/json"})
    return _session


def get_json(url, params=None, max_attempts=6, timeout=25):
    """GET with retry/backoff. Returns parsed JSON or None (non-retriable / exhausted)."""
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            r = session().get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 403, 500, 502, 503, 504, 520, 522):
                print(f"HTTP {r.status_code} (attempt {attempt}) {url}", file=sys.stderr)
            else:
                print(f"HTTP {r.status_code} (giving up) {url}", file=sys.stderr)
                return None
        except Exception as e:
            print(f"attempt {attempt} failed: {e} {url}", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 20)
    return None


def get_channel(slug):
    return get_json(f"{BASE}/api/v2/channels/{slug}")


def get_channel_videos(slug):
    return get_json(f"{BASE}/api/v2/channels/{slug}/videos") or []


def find_vod(slug, uuid):
    """VOD一覧から video.uuid が一致するエントリを返す。無ければ None。"""
    for v in get_channel_videos(slug):
        vid = v.get("video") or {}
        if vid.get("uuid") == uuid:
            return v
    return None


def get_video_meta_web(channel_id, uuid):
    """web.kick.com フォールバック (duration秒・start_time・title・is_live)。"""
    d = get_json(f"{WEB_BASE}/api/v1/channels/{channel_id}/videos/{uuid}")
    if isinstance(d, dict) and "data" in d:
        return d["data"]
    return d


def get_chat_window(channel_id, iso_ts):
    """start_time 以降の直近メッセージ群 (約5秒窓/25件)。v2 -> web.kick フォールバック。"""
    d = get_json(f"{BASE}/api/v2/channels/{channel_id}/messages",
                 params={"start_time": iso_ts}, max_attempts=4)
    if d is None:
        d = get_json(f"{WEB_BASE}/api/v1/chat/{channel_id}/history",
                     params={"start_time": iso_ts}, max_attempts=4)
    if not d:
        return []
    return (d.get("data") or {}).get("messages") or []


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
