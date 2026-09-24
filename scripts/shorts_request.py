"""workflow_dispatch の入力から queue_shorts/request.json を組み立てる。

区間(segments)は書かない。空のまま shorts_pick に渡すと、チャットの盛り上がりから
自動で選ばれる。手で区間を決めたいときは、これを使わずに request.json を push する
（その依頼は shorts_pick が素通しする）。

  python scripts/shorts_request.py --video <URL> [--platform kick|twitch|youtube] [--n 8]
"""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def youtube_video_id(video):
    """Return an ID for supported public YouTube watch and short URLs."""
    try:
        parts = urlsplit(video)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        return ""
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if parts.path != "/watch":
            return ""
        values = parse_qs(parts.query).get("v", [])
        vid = values[0] if values else ""
    elif host in {"youtu.be", "www.youtu.be"}:
        vid = parts.path.strip("/").split("/", 1)[0]
    else:
        return ""
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) else ""


def guess_platform(video):
    if youtube_video_id(video):
        return "youtube"
    if "twitch.tv" in video:
        return "twitch"
    if "kick.com" in video:
        return "kick"
    return "kick"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--platform", default="")
    ap.add_argument("--n", default="8")
    ap.add_argument("--height", default="720")
    ap.add_argument("--out", default="queue_shorts/request.json")
    a = ap.parse_args()

    video = a.video.strip()
    if not re.match(r"^https?://", video):
        raise SystemExit("error: URL が不正: " + video)
    platform = (a.platform or "").strip() or guess_platform(video)
    if platform not in ("kick", "twitch", "youtube"):
        raise SystemExit("error: platform は kick / twitch / youtube: " + platform)
    if platform == "youtube" and not youtube_video_id(video):
        raise SystemExit("error: youtube platform には watch URL または youtu.be URL が必要です")

    req = {
        "platform": platform,
        "video": video,
        "height": int(a.height or 720),
        "n": int(a.n or 8),
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    print("request: " + platform + " n=" + str(req["n"]) + " " + video)


if __name__ == "__main__":
    main()
