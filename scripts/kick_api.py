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
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

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


def get_chat_window(channel_id, iso_ts, *, strict=False, receipt=None):
    """start_time 以降の直近メッセージ群 (約5秒窓/25件)。v2 -> web.kick フォールバック。"""
    if strict:
        return get_chat_window_strict(channel_id, iso_ts, receipt=receipt)
    d = get_json(f"{BASE}/api/v2/channels/{channel_id}/messages",
                 params={"start_time": iso_ts}, max_attempts=4)
    if d is None:
        d = get_json(f"{WEB_BASE}/api/v1/chat/{channel_id}/history",
                     params={"start_time": iso_ts}, max_attempts=4)
    if not d:
        return []
    return (d.get("data") or {}).get("messages") or []


_strict_last_request = None
def retry_after_seconds(value, now=None):
    if not value: return None
    try: return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None: date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date-(now or datetime.now(timezone.utc))).total_seconds())
        except (ValueError, TypeError, OverflowError): return None


def get_chat_window_strict(channel_id, iso_ts, *, receipt=None, max_attempts=4, min_interval=1.0):
    """Recovery only: one fixed endpoint, paced sequential calls; failures never empty."""
    global _strict_last_request
    for attempt in range(1, max_attempts+1):
        if _strict_last_request is not None:
            time.sleep(max(0.0, min_interval-(time.monotonic()-_strict_last_request)))
        _strict_last_request = time.monotonic()
        response = None
        try:
            response = session().get(f"{BASE}/api/v2/channels/{channel_id}/messages", params={'start_time':iso_ts}, timeout=25)
        except Exception as error:
            if attempt == max_attempts: raise RuntimeError('Strict chat transport exhausted') from error
        if response is not None and response.status_code == 200:
            try: value = response.json()
            except Exception as error: raise RuntimeError('Strict chat invalid JSON') from error
            data = value.get('data') if isinstance(value, dict) else None
            status = value.get('status') if isinstance(value, dict) else None
            if isinstance(status, dict) and (status.get('error') is not False or status.get('code') != 200):
                raise RuntimeError('Strict chat API unsuccessful status')
            messages = data.get('messages') if isinstance(data, dict) else None
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise RuntimeError('Strict chat invalid messages schema')
            if receipt is not None:
                receipt.update({'responseValid':True,'count':len(messages),'attempts':attempt,'cursor':data.get('cursor'),
                                'warnings':['api-message-cap-possible'] if len(messages)>=25 else []})
            return messages
        if response is not None and response.status_code not in (429,500,502,503,504,520,522):
            raise RuntimeError(f'Strict chat HTTP {response.status_code}')
        if attempt == max_attempts: raise RuntimeError('Strict chat retry exhausted')
        delay = retry_after_seconds(response.headers.get('Retry-After')) if response is not None else None
        delay = delay if delay is not None else 60.0
        if delay > 300: raise RuntimeError('Strict chat Retry-After exceeds recovery retry budget; stop without retry')
        print(f'strict chat retry {attempt}/{max_attempts} delay={delay:.1f}s', file=sys.stderr, flush=True)
        time.sleep(delay)
    raise RuntimeError('Strict chat failed')


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
