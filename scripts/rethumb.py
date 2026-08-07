"""投稿済み動画のサムネイル再生成 (絵文字豆腐の修理など)。

maintenance/rethumb.txt に uuid を1行1件書いて push すると rethumb.yml が起動する。
各動画: チャット再取得→ピーク検出→フレーム取得→新composeで再合成→thumbnails.set。
Kickのsourceが失効している場合はYouTube側の動画からフレームを取る。

    python rethumb.py [--targets maintenance/rethumb.txt] [--dry-run]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import chat_fetch
import kick_api
import thumbnail
from plan import resolve_meta
from repo_state import STATE_DIR
from yt_upload import channel_settings, get_credentials, set_thumbnail


def fetch_frame_via_youtube(yt_url, t_sec, out_png, clip="ytclip.mp4"):
    """source失効VODのフォールバック: YouTube動画のピーク付近だけ低画質DLして抜く。"""
    Path(clip).unlink(missing_ok=True)
    t = max(0, int(t_sec))
    subprocess.run(["yt-dlp", "-f", "wv*/w", "--no-part",
                    "--download-sections", f"*{t}-{t + 20}",
                    "-o", clip, yt_url], check=True)
    thumbnail.fetch_frame_from_video(clip, 5, out_png)
    Path(clip).unlink(missing_ok=True)


def process_one(uuid, st, cfg, font, emoji_font, dry_run):
    slug = st["slug"]
    meta = None
    try:
        meta = resolve_meta(slug, uuid)
    except Exception as e:
        print(f"resolve_meta failed ({e}) — using state fields", file=sys.stderr)
    title = (meta or st)["title"]
    start_time = (meta or st)["start_time"]
    duration_s = float((meta or st).get("duration_s") or st["duration_s"])
    source = (meta or {}).get("source")
    channel_id = (meta or {}).get("channel_id")
    print(f"target: {title} ({st['yt_url']})", file=sys.stderr)

    # チャットからピーク検出 (取れなければ尺の1/3で妥協)
    peak = duration_s / 3
    if channel_id:
        try:
            msgs = chat_fetch.fetch_all_chat(
                channel_id, chat_fetch.parse_dt(start_time), duration_s,
                workers=cfg.get("chat_workers", 8), keep_emotes=False)
            chat_path = f"chat_{uuid[:8]}.jsonl"
            with open(chat_path, "w", encoding="utf-8") as f:
                for rel, _content in msgs:
                    f.write(json.dumps({"rel": rel}) + "\n")
            peak = thumbnail.find_hype_peak(chat_path, duration_s)
            print(f"hype peak: {peak:.0f}s ({len(msgs)} msgs)", file=sys.stderr)
        except Exception as e:
            print(f"chat fetch failed ({e}) — fallback peak {peak:.0f}s",
                  file=sys.stderr)

    frame = f"frame_{uuid[:8]}.png"
    try:
        if not source:
            raise RuntimeError("no source url")
        thumbnail.fetch_frame_from_source(source, peak, frame)
    except Exception as e:
        print(f"source frame failed ({e}) — YouTube frame fallback", file=sys.stderr)
        fetch_frame_via_youtube(st["yt_url"], peak, frame, clip=f"clip_{uuid[:8]}.mp4")

    out = f"thumb_{uuid[:8]}.jpg"
    date_slash = str(start_time)[:10].replace("-", "/")
    thumbnail.compose(frame, title, date_slash, font, out, emoji_font_path=emoji_font)
    if dry_run:
        print(f"dry-run: generated {out}")
        return
    video_id = st["yt_url"].split("v=")[-1]
    creds = get_credentials(channel_settings(cfg, slug)["yt_token_env"])
    set_thumbnail(creds, video_id, out)
    print(f"rethumb done: {video_id} {title}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--targets", default="maintenance/rethumb.txt")
    ap.add_argument("--font", default="fonts/BIZUDPGothic-Regular.ttf")
    ap.add_argument("--emoji-font", default="fonts/NotoColorEmoji.ttf")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)

    targets = [ln.strip() for ln in
               Path(a.targets).read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
    if not targets:
        print("no targets")
        return

    ok = failed = 0
    for uuid in targets:
        try:
            st = json.loads((STATE_DIR / f"{uuid}.json").read_text(encoding="utf-8"))
            if st.get("status") != "done" or "v=" not in (st.get("yt_url") or ""):
                print(f"skip {uuid}: not done/uploaded", file=sys.stderr)
                continue
            process_one(uuid, st, cfg, a.font, a.emoji_font, a.dry_run)
            ok += 1
        except Exception as e:
            print(f"FAILED {uuid}: {e}", file=sys.stderr)
            failed += 1
    print(f"done: ok={ok} failed={failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
