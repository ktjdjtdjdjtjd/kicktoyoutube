"""workflow_dispatch の入力から queue_shorts/request.json を組み立てる。

区間(segments)は書かない。空のまま shorts_pick に渡すと、チャットの盛り上がりから
自動で選ばれる。手で区間を決めたいときは、これを使わずに request.json を push する
（その依頼は shorts_pick が素通しする）。

  python scripts/shorts_request.py --video <URL> [--platform kick] [--n 8]
"""
import argparse
import json
import re
from pathlib import Path


def guess_platform(video):
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
    if platform not in ("kick", "twitch"):
        raise SystemExit("error: platform は kick か twitch: " + platform)

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
