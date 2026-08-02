"""process の第2段 (セグメント並列): VODをDLし、担当区間だけ焼き込みエンコードする。

    python burn.py <slug> <uuid> <seg_start> <seg_end> \
        --ass out/full.ass --out seg_000.mp4 [--config config.json] [--fontsdir fonts]

やること:
  1. yt-dlp (--impersonate chrome) で VOD 全体を config.format_height 以下でDL
  2. slice_ass で担当区間の ASS を切り出し (時刻シフト + 境界コメントの位置補間)
  3. ffmpeg -ss/-t 入力シーク + subtitles=…:fontsdir=… で焼き込み
     (libx264 / yuv420p / faststart / timescale 90000 — 結合前提の共通パラメータ)
"""
import argparse
import subprocess
import sys
from pathlib import Path

import kick_api
import slice_ass as slicer


def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def download_vod(url, height, dest):
    fmt = f"bv*[height<=?{height}]+ba/b[height<=?{height}]/bv*+ba/b"
    run([
        "yt-dlp", "--impersonate", "chrome",
        "-f", fmt,
        "--no-part", "--retries", "20", "--fragment-retries", "50",
        "--concurrent-fragments", "4",
        "--merge-output-format", "mp4",
        "-o", dest, url,
    ])


def burn_segment(vod, ass_path, seg_start, seg_end, out, preset, crf, fontsdir):
    dur = seg_end - seg_start
    seg_ass = Path(out).with_suffix(".ass")
    with open(ass_path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    sliced, n = slicer.slice_ass(lines, seg_start, seg_end)
    seg_ass.write_text("\n".join(sliced), encoding="utf-8")
    print(f"segment ass: {n} events", file=sys.stderr)

    def fpath(p):
        # ffmpegフィルタ引数用エスケープ (Windows絶対パスの ':' 等)
        return Path(p).as_posix().replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    vf = f"subtitles=filename='{fpath(seg_ass)}'"
    if fontsdir:
        vf += f":fontsdir='{fpath(fontsdir)}'"
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
        "-ss", f"{seg_start:.3f}", "-t", f"{dur:.3f}", "-i", vod,
        "-vf", vf,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-video_track_timescale", "90000",
        "-c:a", "aac", "-b:a", "160k", "-af", "aresample=async=1:first_pts=0",
        "-movflags", "+faststart",
        out,
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("uuid")
    ap.add_argument("seg_start", type=float)
    ap.add_argument("seg_end", type=float)
    ap.add_argument("--ass", default="out/full.ass")
    ap.add_argument("--out", default="seg.mp4")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--fontsdir", default="fonts")
    ap.add_argument("--vod", default="vod.mp4", help="DL先 (既存ならDLスキップ)")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)

    url = f"https://kick.com/{a.slug}/videos/{a.uuid}"
    if not Path(a.vod).exists():
        download_vod(url, cfg.get("format_height", 720), a.vod)
    else:
        print(f"vod exists, skip download: {a.vod}", file=sys.stderr)

    burn_segment(a.vod, a.ass, a.seg_start, a.seg_end, a.out,
                 cfg.get("encode_preset", "veryfast"), str(cfg.get("encode_crf", "23")),
                 a.fontsdir)
    print(f"done: {a.out}")


if __name__ == "__main__":
    main()
