"""burnonly (コメ焼きオンリー) の第1段: 依頼解決 + チャットDL + 分割計画。

YouTubeへは上げず、焼き上がりを成果物(artifact)として返すフロー用。
依頼は queue_burn/request.json か CLI引数:

  {"platform": "kick" | "twitch",
   "video": "<VOD URL>",          # kick.com/<slug>/videos/<uuid> / twitch.tv/videos/<id>
   "height": 720,                 # 省略可
   "limit_seconds": 0}            # >0 でスモークテスト (先頭N秒のみ)

    python burn_request.py [--request queue_burn/request.json] \
        [--platform kick --video URL] [--out out]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import chat_fetch
import emotes as emotes_mod
import kick_api
from plan import gh_output, plan_segments, resolve_meta

KICK_URL_RE = re.compile(r"kick\.com/([^/]+)/videos/([0-9a-f-]{16,})")
TWITCH_URL_RE = re.compile(r"twitch\.tv/videos/(\d+)")


def parse_request(a):
    if a.video:
        return {"platform": a.platform, "video": a.video,
                "height": a.height, "limit_seconds": a.limit_seconds}
    req = json.loads(Path(a.request).read_text(encoding="utf-8"))
    req.setdefault("height", 720)
    req.setdefault("limit_seconds", 0)
    return req


def safe_name(title, fallback="video"):
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")
    return (s[:60] or fallback)


def plan_kick(video, cfg, outdir, limit_seconds):
    m = KICK_URL_RE.search(video)
    if not m:
        sys.exit(f"error: kick URL が解釈できない: {video}")
    slug, uuid = m.group(1), m.group(2)
    meta = resolve_meta(slug, uuid)
    if meta["is_live"]:
        sys.exit("error: VOD is still live")
    duration = meta["duration_s"]
    if limit_seconds:
        duration = min(duration, limit_seconds)
    start_dt = chat_fetch.parse_dt(meta["start_time"])
    msgs = chat_fetch.fetch_all_chat(meta["channel_id"], start_dt, duration,
                                     workers=cfg.get("chat_workers", 8),
                                     keep_emotes=True)
    # エモート: リポジトリ蓄積分を使い、新顔はDL (コミットは呼び出し側workflowが不要なら省略)
    ids = emotes_mod.collect_ids(msgs)
    emotes_mod.download_missing(ids, "emotes", session=kick_api.session())
    seg_emotes = outdir / "emotes"
    seg_emotes.mkdir(exist_ok=True)
    for eid in ids:
        src = emotes_mod.find_file("emotes", eid)
        if src:
            shutil.copy2(src, seg_emotes / src.name)
    return {
        "slug": slug, "uuid": uuid,
        "url": f"https://kick.com/{slug}/videos/{uuid}",
        "source": meta.get("source"),
        "title": meta["title"], "duration_s": duration,
        "start_time": meta["start_time"], "date": str(meta["start_time"])[:10],
    }, msgs


def plan_twitch(video, outdir, limit_seconds):
    m = TWITCH_URL_RE.search(video)
    vid = m.group(1) if m else video.lstrip("v")
    if not vid.isdigit():
        sys.exit(f"error: twitch VOD id が解釈できない: {video}")
    url = f"https://www.twitch.tv/videos/{vid}"
    r = subprocess.run(["yt-dlp", "--dump-single-json", "--no-warnings", url],
                       capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    duration = float(info["duration"])
    if limit_seconds:
        duration = min(duration, limit_seconds)
    from twitch_chat_fetch import fetch_comments
    msgs = fetch_comments(vid, max_seconds=duration)
    print(f"twitch chat: {len(msgs)} msgs", file=sys.stderr)
    (outdir / "emotes").mkdir(exist_ok=True)  # 空 (Twitchはテキストのみ v1)
    return {
        "slug": "twitch", "uuid": vid, "url": url,
        "source": url,  # burn.py はこのURLをyt-dlpに渡す
        "title": info.get("title") or f"twitch_{vid}",
        "duration_s": duration,
        "start_time": info.get("upload_date") or "", "date": info.get("upload_date") or "",
    }, msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", default="queue_burn/request.json")
    ap.add_argument("--platform", default="", choices=["", "kick", "twitch"])
    ap.add_argument("--video", default="")
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--limit-seconds", type=int, default=0)
    ap.add_argument("--out", default="out")
    ap.add_argument("--config", default="config.json")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)

    req = parse_request(a)
    limit = int(req.get("limit_seconds") or 0)
    print(f"request: {req}", file=sys.stderr)

    if req["platform"] == "kick":
        meta, msgs = plan_kick(req["video"], cfg, outdir, limit)
    elif req["platform"] == "twitch":
        meta, msgs = plan_twitch(req["video"], outdir, limit)
    else:
        sys.exit(f"error: unknown platform: {req.get('platform')}")

    with open(outdir / "chat.jsonl", "w", encoding="utf-8") as f:
        for rel, content in msgs:
            f.write(json.dumps({"rel": rel, "content": content},
                               ensure_ascii=False) + "\n")

    meta["segments"] = plan_segments(meta["duration_s"],
                                     cfg.get("segment_seconds", 5400))
    meta["height"] = int(req.get("height") or 720)
    meta["n_messages"] = len(msgs)
    (outdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    gh_output("matrix", json.dumps(meta["segments"]))
    gh_output("slug", meta["slug"])
    gh_output("uuid", meta["uuid"])
    gh_output("name", safe_name(meta["title"], meta["uuid"]))
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
