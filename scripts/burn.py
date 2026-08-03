"""process の第2段 (セグメント並列): VODをDLし、担当区間へダンマクを焼き込む。

    python burn.py <slug> <uuid> <seg_start> <seg_end> \
        --chat out/chat.jsonl --emotes out/emotes --meta out/meta.json \
        --font fonts/BIZUDPGothic-Regular.ttf --out seg_000.mp4

やること:
  1. yt-dlp (--impersonate chrome) で VOD 全体を config.format_height 以下でDL
     (metaのsource m3u8を直渡し。kick抽出器は新v7 uuidで404するため)
  2. strip_render でレーン別ストリップPNG (テキスト+エモート画像) を生成
  3. ffmpeg -ss/-t 入力シーク + overlay×レーン数 で合成エンコード
     (libx264 / yuv420p / faststart / timescale 90000 — 結合前提の共通パラメータ)
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import kick_api
import strip_render


def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def download_vod(url, height, dest, attempts=3):
    fmt = f"bv*[height<=?{height}]+ba/b[height<=?{height}]/bv*+ba/b"
    cmd = [
        "yt-dlp", "--impersonate", "chrome",
        "-f", fmt,
        "--no-part", "--retries", "20", "--fragment-retries", "50",
        "--concurrent-fragments", "2",
        "--socket-timeout", "30",
        "--merge-output-format", "mp4",
        "-o", dest, url,
    ]
    import time
    for i in range(1, attempts + 1):
        try:
            run(cmd)
            return
        except subprocess.CalledProcessError:
            if i == attempts:
                raise
            print(f"download attempt {i} failed — retry in {90*i}s", file=sys.stderr)
            Path(dest).unlink(missing_ok=True)
            time.sleep(90 * i)


def probe_dims(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    w, h = r.stdout.strip().split("\n")[0].split(",")
    return int(w), int(h)


def burn_segment(vod, chat_jsonl, seg_start, seg_end, out, preset, crf,
                 font_path, emote_dir, strips_dir="strips"):
    dur = seg_end - seg_start
    vw, vh = probe_dims(vod)
    scale = vh / 1080.0
    print(f"video {vw}x{vh} scale={scale:.3f}", file=sys.stderr)
    manifest = strip_render.build_for_segment(
        chat_jsonl, seg_start, seg_end, font_path, emote_dir, strips_dir, scale=scale)
    strips = manifest["strips"]
    lm = manifest["left_margin"]
    speed = manifest["speed"]

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
           "-ss", f"{seg_start:.3f}", "-t", f"{dur:.3f}", "-i", vod]
    for s in strips:
        cmd += ["-i", str(Path(strips_dir) / s["file"])]
    if strips:
        chains = []
        prev = "[0:v]"
        for i, s in enumerate(strips):
            lbl = f"[v{i+1}]"
            chains.append(
                f"{prev}[{i+1}:v]overlay=x={vw}-{lm}-t*{speed}:y={s['y']}:eof_action=repeat{lbl}")
            prev = lbl
        cmd += ["-filter_complex", ";".join(chains), "-map", prev]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-map", "0:a?",
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-video_track_timescale", "90000",
            "-c:a", "aac", "-b:a", "160k", "-af", "aresample=async=1:first_pts=0",
            "-movflags", "+faststart",
            out]
    run(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("uuid")
    ap.add_argument("seg_start", type=float)
    ap.add_argument("seg_end", type=float)
    ap.add_argument("--chat", default="out/chat.jsonl")
    ap.add_argument("--emotes", default="out/emotes")
    ap.add_argument("--meta", default="out/meta.json", help="plan出力 (sourceのm3u8を使う)")
    ap.add_argument("--font", default="fonts/BIZUDPGothic-Regular.ttf")
    ap.add_argument("--out", default="seg.mp4")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--vod", default="vod.mp4", help="DL先 (既存ならDLスキップ)")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)

    url = f"https://kick.com/{a.slug}/videos/{a.uuid}"
    try:
        source = json.loads(Path(a.meta).read_text(encoding="utf-8")).get("source")
        if source:
            url = source
    except Exception as e:
        print(f"meta read failed ({e}) — fall back to page URL", file=sys.stderr)
    if not Path(a.vod).exists():
        download_vod(url, cfg.get("format_height", 720), a.vod)
    else:
        print(f"vod exists, skip download: {a.vod}", file=sys.stderr)

    burn_segment(a.vod, a.chat, a.seg_start, a.seg_end, a.out,
                 cfg.get("encode_preset", "veryfast"), str(cfg.get("encode_crf", "23")),
                 a.font, a.emotes)
    print(f"done: {a.out}")


if __name__ == "__main__":
    main()
