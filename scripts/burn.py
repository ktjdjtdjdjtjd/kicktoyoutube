"""process の第2段 (セグメント並列): VODをDLし、担当区間へダンマクを焼き込む。

    python burn.py <slug> <uuid> <seg_start> <seg_end> \
        --chat out/chat.jsonl --emotes out/emotes --meta out/meta.json \
        --font fonts/BIZUDPGothic-Regular.ttf --out seg_000.mp4

やること:
  1. yt-dlp (--impersonate chrome) で VOD 全体を config.format_height 以下でDL
     (metaのsource m3u8を直渡し。kick抽出器は新v7 uuidで404するため。
     ランナーの残ディスクが足りない場合は config.fallback_height へ自動降格する)
  2. strip_render でレーン別ストリップPNG (テキスト+エモート画像) を生成
  3. ffmpeg -ss/-t 入力シーク + overlay×レーン数 で合成エンコード
     (libx264 / yuv420p / faststart / timescale 90000 — 結合前提の共通パラメータ)
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import kick_api
import strip_render

# ビットレート概算 (kbps基準の実測値。DL(全編)+エンコード出力(担当区間)の両方に使う概算)
BITRATE_BPS = {1080: 6.0e6, 720: 2.4e6, 480: 1.2e6, 360: 0.8e6}


def _bitrate(height):
    return BITRATE_BPS.get(height, 6.0e6)


def _estimate_bytes(height, duration_s, seg_dur_s, margin):
    """VOD全体DL(概算, margin込み) + 担当区間のエンコード出力(概算) の合計バイト数。"""
    br = _bitrate(height)
    return duration_s * br / 8 * margin + seg_dur_s * br / 8


def pick_height(want, fallback, free_bytes, duration_s, seg_dur_s, margin=1.3):
    """ランナーの残ディスクから実際に使う画質を決める。

    want (config.format_height or burnonlyのheight指定) で足りればそのまま、
    足りず fallback の方が低ければ fallback へ降格。duration_s が不明/0なら
    見積り不能なので常に want (チェックをスキップ)。"""
    if not duration_s:
        return want
    need = _estimate_bytes(want, duration_s, seg_dur_s, margin)
    if need <= free_bytes:
        return want
    if fallback < want:
        return fallback
    return want


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


def log_mem(tag):
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            info[k] = v.strip()
        print(f"mem[{tag}]: total={info.get('MemTotal')} avail={info.get('MemAvailable')}",
              file=sys.stderr)
    except Exception:
        pass


def _encode_chunk(vod, chunk_start, chunk_dur, manifest, strips_dir, vw,
                  preset, crf, out):
    log_mem("before-encode")
    strips = manifest["strips"]
    lm = manifest["left_margin"]
    speed = manifest["speed"]
    pfps = manifest.get("gif_phase_fps", 5)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-stats",
           "-ss", f"{chunk_start:.3f}", "-t", f"{chunk_dur:.3f}", "-i", vod]
    entries = []  # (input_index, y, speed, enable_expr or None)
    idx = 1
    for s in strips:
        files = s["files"]
        k_total = len(files)
        for k, fname in enumerate(files):
            cmd += ["-i", str(Path(strips_dir) / fname)]
            enable = None
            if k_total > 1:
                enable = f"eq(mod(floor(t*{pfps})\\,{k_total})\\,{k})"
            entries.append((idx, s["y"], s.get("speed", speed), enable))
            idx += 1
    if entries:
        chains = []
        prev = "[0:v]"
        for n, (i, y, spd, enable) in enumerate(entries):
            lbl = f"[v{n+1}]"
            opts = f"x={vw}-{lm}-t*{spd}:y={y}:eof_action=repeat"
            if enable:
                opts += f":enable='{enable}'"
            chains.append(f"{prev}[{i}:v]overlay={opts}{lbl}")
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


def burn_segment(vod, chat_jsonl, seg_start, seg_end, out, preset, crf,
                 font_path, emote_dir, strips_dir="strips", cfg=None,
                 emoji_font_path=None):
    """セグメントをさらに chunk_seconds (既定30分) 刻みで焼いて結合する。

    ストリップ幅∝速度×時間 かつ ffmpegは全入力フレームを常駐させるため、
    高速設定×GIF位相数だとメモリが跳ねる。チャンク化で常に数GB以下に抑える
    (レーン割当は全体一括なのでチャンク境界でも流れは連続する)。"""
    vw, vh = probe_dims(vod)
    scale = vh / 1080.0
    chunk_seconds = float(((cfg or {}).get("danmaku") or {}).get("chunk_seconds", 1800))
    print(f"video {vw}x{vh} scale={scale:.3f} chunk={chunk_seconds}s", file=sys.stderr)

    chunks = []
    t = seg_start
    while t < seg_end:
        chunks.append((t, min(t + chunk_seconds, seg_end)))
        t += chunk_seconds
    # 端数チャンクが5分未満なら前と併合
    if len(chunks) >= 2 and (chunks[-1][1] - chunks[-1][0]) < 300:
        chunks[-2] = (chunks[-2][0], chunks[-1][1])
        chunks.pop()

    parts = []
    for ci, (cs, ce) in enumerate(chunks):
        cdir = f"{strips_dir}_c{ci}"
        manifest = strip_render.build_for_segment(
            chat_jsonl, cs, ce, font_path, emote_dir, cdir,
            scale=scale, cfg=cfg, emoji_font_path=emoji_font_path)
        part = f"chunk_{ci:03d}.mp4"
        _encode_chunk(vod, cs, ce - cs, manifest, cdir, vw, preset, crf, part)
        parts.append(part)
        import shutil as _sh
        _sh.rmtree(cdir, ignore_errors=True)

    if len(parts) == 1:
        Path(parts[0]).replace(out)
    else:
        lst = Path("chunks.txt")
        lst.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
             "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", out])
        for p in parts:
            Path(p).unlink(missing_ok=True)


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
    ap.add_argument("--emoji-font", default="fonts/NotoColorEmoji.ttf")
    ap.add_argument("--out", default="seg.mp4")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--vod", default="vod.mp4", help="DL先 (既存ならDLスキップ)")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)

    url = f"https://kick.com/{a.slug}/videos/{a.uuid}"
    want = cfg.get("format_height", 720)
    fallback = cfg.get("fallback_height", 720)
    duration_s = 0
    try:
        meta_d = json.loads(Path(a.meta).read_text(encoding="utf-8"))
        if meta_d.get("source"):
            url = meta_d["source"]
        # burnonly依頼はmetaにheight指定を持つ (アーカイブフローのmetaには無く720のまま)
        want = int(meta_d.get("height") or want)
        duration_s = meta_d.get("duration_s") or 0
    except Exception as e:
        print(f"meta read failed ({e}) — fall back to page URL", file=sys.stderr)

    seg_dur_s = a.seg_end - a.seg_start
    free_bytes = shutil.disk_usage(".").free
    height = pick_height(want, fallback, free_bytes, duration_s, seg_dur_s)
    need_bytes = _estimate_bytes(want, duration_s, seg_dur_s, 1.3) if duration_s else 0
    if not duration_s:
        reason = "duration unknown, skip check"
    elif height == want:
        reason = "fits in free space"
    else:
        reason = "disk-limited, falling back"
    print(f"height: want={want} free={free_bytes/1e9:.1f}GB need={need_bytes/1e9:.1f}GB "
          f"-> {height} ({reason})", file=sys.stderr)

    if not Path(a.vod).exists():
        download_vod(url, height, a.vod)
    else:
        print(f"vod exists, skip download: {a.vod}", file=sys.stderr)

    burn_segment(a.vod, a.chat, a.seg_start, a.seg_end, a.out,
                 cfg.get("encode_preset", "veryfast"), str(cfg.get("encode_crf", "23")),
                 a.font, a.emotes, cfg=cfg, emoji_font_path=a.emoji_font)
    print(f"done: {a.out}")


if __name__ == "__main__":
    main()
