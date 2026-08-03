"""ヘッドレスYouTubeアップロード (GitHub Actions用)。

認証: 環境変数 YT_TOKEN_JSON に authorized-user 形式の token JSON
(client_id / client_secret / refresh_token を含む。既存の
 stream-chat-burn/youtube_upload.py --auth-only で作った youtube_token_*.json の中身)。

    python yt_upload.py final.mp4 --meta out/meta.json --config config.json
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def sanitize_title(t):
    t = t.replace("<", "＜").replace(">", "＞").strip()
    return t[:95] if len(t) > 95 else t


def get_credentials():
    raw = os.environ.get("YT_TOKEN_JSON", "")
    if not raw.strip():
        sys.exit("error: env YT_TOKEN_JSON is empty — set repo secret")
    info = json.loads(raw)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            sys.exit("error: token has no refresh_token")
        creds.refresh(Request())
    return creds


def upload(creds, path, title, description, tags, category_id, privacy):
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(path, chunksize=16 * 1024 * 1024, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    last = -1
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504):
                print(f"retriable HTTP {e.resp.status}, retrying...", file=sys.stderr)
                time.sleep(10)
                continue
            raise
        if status:
            pct = int(status.progress() * 100)
            if pct // 10 != last // 10:
                print(f"upload: {pct}%", file=sys.stderr)
                last = pct
    return response


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--meta", default="out/meta.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--privacy", default="", help="config の privacy を上書き")
    a = ap.parse_args()
    meta = json.loads(Path(a.meta).read_text(encoding="utf-8"))
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))

    fields = {
        "date": meta["date"],
        "date_slash": str(meta["date"]).replace("-", "/"),
        "title": meta["title"],
        "channel": meta["slug"],
        "url": meta["url"],
    }
    title = sanitize_title(cfg["title_template"].format(**fields))
    description = cfg["description_template"].format(**fields)
    privacy = a.privacy or cfg.get("privacy", "unlisted")

    creds = get_credentials()
    print(f"uploading: {a.video}\n  title: {title}\n  privacy: {privacy}", file=sys.stderr)
    resp = upload(creds, a.video, title, description,
                  cfg.get("tags", []), cfg.get("category_id", "24"), privacy)
    vid = resp.get("id")
    out = {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": title}
    print(json.dumps(out, ensure_ascii=False))
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"yt_url={out['url']}\n")


if __name__ == "__main__":
    main()
