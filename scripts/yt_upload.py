"""ヘッドレスYouTubeアップロード (GitHub Actions用)。

認証: 環境変数 YT_TOKEN_JSON に authorized-user 形式の token JSON
(client_id / client_secret / refresh_token を含む。既存の
 stream-chat-burn/youtube_upload.py --auth-only で作った youtube_token_*.json の中身)。

    python yt_upload.py final.mp4 --meta out/meta.json --config config.json
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# YouTubeの上限は12時間。これを超えるとAPIはIDを返すが処理段階で rejected(tooLong) になる。
# 分割はキーフレーム境界で切れて指定より少し長くなるため、余裕を持って11.5時間で切る。
MAX_UPLOAD_SECONDS = 11.5 * 3600


def sanitize_title(t):
    t = t.replace("<", "＜").replace(">", "＞").strip()
    return t[:95] if len(t) > 95 else t


def part_title(title, i, n):
    """分割投稿のタイトル: 末尾に（i/N）。95字上限を接尾辞込みで守る。"""
    suffix = f"（{i}/{n}）"
    return sanitize_title(title)[:95 - len(suffix)] + suffix


def channel_settings(cfg, slug):
    """Kickチャンネルごとの投稿設定 (未定義はトップレベル値へフォールバック)。"""
    cs = (cfg.get("channel_settings") or {}).get(slug, {})
    return {
        "yt_token_env": cs.get("yt_token_env", "YT_TOKEN_JSON"),
        "title_template": cs.get("title_template", cfg["title_template"]),
        "description_template": cs.get("description_template", cfg["description_template"]),
        "tags": cs.get("tags", cfg.get("tags", [])),
    }


def get_credentials(env_name="YT_TOKEN_JSON"):
    raw = os.environ.get(env_name, "")
    if not raw.strip():
        sys.exit(f"error: env {env_name} is empty — set repo secret")
    info = json.loads(raw)
    # scopesはトークンに記録された付与済みスコープをそのまま使う
    # (固定リストで上書きすると付与外スコープ要求扱いになり invalid_scope で更新失敗する)
    creds = Credentials.from_authorized_user_info(info)
    if not creds.valid:
        if not creds.refresh_token:
            sys.exit("error: token has no refresh_token")
        creds.refresh(Request())
    return creds


QUOTA_MARKERS = ("quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
                 "rateLimitExceeded")


def upload(creds, path, title, description, tags, category_id, privacy):
    """クォータ超過(1日6本上限など)は30分おきに最大9回待ってやり直す。"""
    for attempt in range(1, 10):
        try:
            return _upload_once(creds, path, title, description, tags, category_id, privacy)
        except HttpError as e:
            body = str(e)
            if e.resp.status == 403 and any(m in body for m in QUOTA_MARKERS) and attempt < 9:
                print(f"quota exceeded — wait 30min then retry ({attempt}/9)", file=sys.stderr)
                time.sleep(1800)
                continue
            raise


def _upload_once(creds, path, title, description, tags, category_id, privacy):
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


def probe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def split_for_upload(path, max_s=MAX_UPLOAD_SECONDS):
    """尺が上限を超える動画をキーフレーム境界で等分割する。[(file, start_s)] を返す。
    上限内なら分割せず [(path, 0.0)]。元ファイルは分割後に削除する(ランナーのディスク対策)。"""
    dur = probe_duration(path)
    if dur <= max_s:
        return [(Path(path), 0.0)], dur
    n = math.ceil(dur / max_s)
    seg_t = dur / n
    print(f"動画が長い ({dur / 3600:.2f}h) — YouTube上限12hのため {n} 分割 "
          f"(各 約{seg_t / 3600:.2f}h)", file=sys.stderr)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                    "-i", str(path), "-c", "copy", "-map", "0",
                    "-f", "segment", "-segment_time", f"{seg_t:.3f}",
                    "-reset_timestamps", "1", "-movflags", "+faststart",
                    "part_%03d.mp4"], check=True)
    parts = sorted(Path(".").glob("part_*.mp4"))
    if not parts:
        sys.exit("error: 分割に失敗した (part_*.mp4 が無い)")
    out, acc = [], 0.0
    for p in parts:
        d = probe_duration(p)
        if d > 12 * 3600:
            sys.exit(f"error: 分割後も上限超過 ({p.name} {d / 3600:.2f}h)")
        out.append((p, acc))
        acc += d
    Path(path).unlink(missing_ok=True)  # 元ファイルを消してディスクを空ける
    print("分割: " + ", ".join(f"{p.name}={d[1] / 3600:.2f}h起点" for p, d in
                               zip(parts, out)), file=sys.stderr)
    return out, dur


def wait_upload_accepted(creds, video_id, timeout_s=1800):
    """YouTubeが実際に動画を受理したかを確認する。

    videos.insert はIDを返した時点では「アップロード済み・処理中」でしかなく、
    尺超過・重複などは**この後**に rejected になる。ここを見ないと
    「壊れた動画のURLを成功として記録する」事故が起きる(実際に13.09hで発生)。
    """
    youtube = build("youtube", "v3", credentials=creds)
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        items = youtube.videos().list(part="status", id=video_id).execute().get("items", [])
        if not items:
            sys.exit(f"error: アップロードした動画が見つからない ({video_id})")
        st = items[0]["status"]
        up = st.get("uploadStatus")
        if up != last:
            print(f"uploadStatus: {up}", file=sys.stderr)
            last = up
        if up in ("rejected", "failed"):
            reason = st.get("rejectionReason") or st.get("failureReason") or "unknown"
            sys.exit(f"error: YouTubeが動画を拒否した (uploadStatus={up} reason={reason}) "
                     f"video_id={video_id}")
        if up == "processed":
            return True
        time.sleep(20)
    # 長尺は処理に時間がかかる。uploaded のまま時間切れは異常ではないので通す
    print(f"warning: 処理完了を確認できなかった (uploadStatus={last}) — 続行", file=sys.stderr)
    return False


def set_thumbnail(creds, video_id, thumb_path):
    """カスタムサムネ設定 (要チャンネル認証)。失敗しても致命傷にしない。"""
    youtube = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(str(thumb_path), mimetype="image/jpeg", resumable=False)
    youtube.thumbnails().set(videoId=video_id, media_body=media).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--meta", default="out/meta.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--privacy", default="", help="config の privacy を上書き")
    ap.add_argument("--thumb", default="", help="サムネJPEG (あれば設定)")
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
    cs = channel_settings(cfg, meta["slug"])
    title = sanitize_title(cs["title_template"].format(**fields))
    description = cs["description_template"].format(**fields)
    privacy = a.privacy or cfg.get("privacy", "unlisted")

    creds = get_credentials(cs["yt_token_env"])
    # 12h超はYouTubeが拒否するので、投稿前に分割する
    parts, total_s = split_for_upload(a.video)
    uploaded = []
    for i, (pf, start_s) in enumerate(parts, 1):
        # 分割時のみ （n/N） を付ける。95字上限を超えないよう接尾辞の分だけ切り詰める
        ptitle = part_title(title, i, len(parts)) if len(parts) > 1 else title
        print(f"uploading: {pf}\n  title: {ptitle}\n  privacy: {privacy}\n"
              f"  token: {cs['yt_token_env']}", file=sys.stderr)
        resp = upload(creds, str(pf), ptitle, description,
                      cs["tags"], cfg.get("category_id", "24"), privacy)
        vid = resp.get("id")
        # IDが返っただけでは受理されていない。拒否(尺超過・重複等)をここで捕まえる
        wait_upload_accepted(creds, vid)
        if a.thumb and Path(a.thumb).exists():
            try:
                set_thumbnail(creds, vid, a.thumb)
                print("thumbnail set", file=sys.stderr)
            except Exception as e:
                print(f"thumbnail skipped: {e}", file=sys.stderr)
        uploaded.append({"id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                         "title": ptitle, "start_s": round(start_s, 3),
                         "duration_s": round(probe_duration(pf), 3)})
        pf.unlink(missing_ok=True)  # 投稿済みは消す (長尺分割でディスクが逼迫するため)

    out = {"id": uploaded[0]["id"], "url": uploaded[0]["url"],
           "title": uploaded[0]["title"], "parts": uploaded}
    print(json.dumps(out, ensure_ascii=False))
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"yt_url={out['url']}\n")
            f.write("yt_parts<<__EOF__\n"
                    + json.dumps(uploaded, ensure_ascii=False) + "\n__EOF__\n")


if __name__ == "__main__":
    main()
