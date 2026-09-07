"""Twitch VODチャット取得 (公開GQL・オフセットページング)。

カーソル方式はintegrity token必須のため、contentOffsetSecondsを歩く方式。
ローカル実績のある stream-chat-burn/twitch_chat.py の取得部の移植。

    fetch_comments(video_id, max_seconds=None) -> [(rel_seconds, text)]
"""
import json
import sys
import time
import urllib.error
import urllib.request

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch web公開クライアントID
COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def gql_query(query, max_attempts=6):
    """persistedQueryではない生クエリ版 (VOD一覧/単体メタ取得用。実測で通ることを確認済み)。"""
    body = json.dumps({"query": query}).encode("utf-8")
    delay = 0.5
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(GQL, data=body, headers={
            "Client-ID": CLIENT_ID,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 15)
                continue
            print(f"HTTPError {e.code}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 15)
    return None


def get_channel_videos(login, first=100):
    """ログイン名のARCHIVE VOD一覧を、kick_api.get_channel_videos()と同じ形に整形して返す。

    watch.py の pick_dispatches はこの形 (video.uuid / start_time / duration[ms] /
    session_title / is_live) しか見ないので、呼び出し側はプラットフォーム差分を
    意識しなくてよい。
    """
    q = ('query{user(login:"%s"){videos(first:%d, type:ARCHIVE){edges{node{'
         'id title publishedAt lengthSeconds}}}}}' % (login, first))
    res = gql_query(q)
    user = ((res or {}).get("data") or {}).get("user")
    if not user:
        return []
    out = []
    for edge in (user.get("videos") or {}).get("edges") or []:
        node = edge.get("node") or {}
        vid = node.get("id")
        if not vid:
            continue
        out.append({
            "video": {"uuid": vid},
            "start_time": node.get("publishedAt"),
            "duration": int(node.get("lengthSeconds") or 0) * 1000,
            "session_title": node.get("title") or "",
            "is_live": False,
        })
    return out


def get_video_meta(video_id):
    """VOD単体メタ (plan.py の resolve_meta 用)。"""
    q = ('query{video(id:"%s"){id title publishedAt lengthSeconds broadcastType '
         'owner{id login}}}' % video_id)
    res = gql_query(q)
    return ((res or {}).get("data") or {}).get("video")


def gql_post(body, max_attempts=6):
    data = json.dumps(body).encode("utf-8")
    delay = 0.5
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(GQL, data=data, headers={
            "Client-ID": CLIENT_ID,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        }, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 15)
                continue
            print(f"HTTPError {e.code}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 15)
    return None


def fetch_comments(video_id, max_seconds=None):
    all_msgs = []
    seen_ids = set()
    offset = 0
    page = 0
    NUDGE_STEP = 5
    MAX_PAGES = 20000  # 安全上限 (~12hで4500ページ程度)
    while page < MAX_PAGES:
        page += 1
        body = [{
            "operationName": "VideoCommentsByOffsetOrCursor",
            "variables": {"videoID": str(video_id), "contentOffsetSeconds": offset},
            "extensions": {"persistedQuery": {"version": 1,
                                              "sha256Hash": COMMENTS_HASH}},
        }]
        res = gql_post(body)
        if not res or not isinstance(res, list) or not res[0].get("data"):
            print(f"page {page}: bad response, stop", file=sys.stderr)
            break
        comments = (res[0]["data"].get("video") or {}).get("comments")
        if not comments:
            break  # VOD終端
        edges = comments.get("edges") or []
        if not edges:
            break
        page_max_offset = 0
        for edge in edges:
            node = edge.get("node") or {}
            rel = node.get("contentOffsetSeconds")
            if rel is None:
                continue
            page_max_offset = max(page_max_offset, rel)
            mid = node.get("id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            frags = (node.get("message") or {}).get("fragments") or []
            text = "".join(f.get("text") or "" for f in frags)
            text = text.replace("\n", " ").strip()
            if text:
                all_msgs.append((float(rel), text))
        if page % 100 == 0:
            print(f"page {page}: offset {offset}, total {len(all_msgs)}",
                  file=sys.stderr)
        if max_seconds is not None and offset > max_seconds:
            break
        offset = page_max_offset + 1 if page_max_offset > offset else offset + NUDGE_STEP
        time.sleep(0.03)
    all_msgs.sort(key=lambda x: x[0])
    if max_seconds is not None:
        all_msgs = [m for m in all_msgs if m[0] <= max_seconds]
    return all_msgs
