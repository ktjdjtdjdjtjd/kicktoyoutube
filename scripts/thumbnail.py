"""サムネイル自動生成。

チャット密度が最大の60秒窓を「盛り上がりピーク」とみなし、その時刻のフレームを
Kickのsource m3u8から直接取得(クリーンな1セグメントのみDL)。
タイトル帯(上部)+配信日(右下)を合成して1280x720のJPEGを出力する。

    python thumbnail.py --meta out/meta.json --chat out/chat.jsonl \
        --font fonts/BIZUDPGothic-Regular.ttf --out thumb.jpg [--fallback-video final.mp4]
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 720
MARGIN = 24


def find_hype_peak(chat_jsonl, duration_s, window=60, guard=300):
    """メッセージ数が最大の60秒窓の中央時刻を返す。冒頭/末尾guard秒は避ける。"""
    from collections import Counter
    counts = Counter()
    with open(chat_jsonl, encoding="utf-8") as f:
        for line in f:
            try:
                rel = json.loads(line)["rel"]
            except Exception:
                continue
            counts[int(rel // window)] += 1
    lo = guard // window
    hi = max(lo + 1, int((duration_s - guard) // window))
    best = None
    for b, n in counts.items():
        if lo <= b <= hi and (best is None or n > counts[best]):
            best = b
    if best is None:
        return duration_s / 3
    return best * window + window / 2


def fetch_frame_from_source(source_url, t_sec, out_png):
    """source master.m3u8 から t_sec 付近のセグメント1本だけDLしてフレームを抜く。"""
    import kick_api
    s = kick_api.session()
    m = s.get(source_url, timeout=20)
    if m.status_code != 200:
        raise RuntimeError(f"master {m.status_code}")
    variants = [l for l in m.text.splitlines() if l.endswith(".m3u8")]
    # 720p優先、無ければ先頭
    var = next((v for v in variants if "720" in v), variants[0])
    base = source_url.rsplit("/", 1)[0]
    v = s.get(f"{base}/{var}", timeout=20)
    if v.status_code != 200:
        raise RuntimeError(f"variant {v.status_code}")
    lines = v.text.splitlines()
    # EXTINFで累積時刻を計算して t_sec を含むセグメントを選ぶ
    segs = []
    acc = 0.0
    dur = 0.0
    for ln in lines:
        if ln.startswith("#EXTINF:"):
            dur = float(ln.split(":")[1].split(",")[0])
        elif ln and not ln.startswith("#"):
            segs.append((acc, ln))
            acc += dur
    target = segs[0][1]
    for start, name in segs:
        if start <= t_sec:
            target = name
            offset = t_sec - start
        else:
            break
    vdir = var.rsplit("/", 1)[0] if "/" in var else ""
    seg_url = f"{base}/{vdir}/{target}" if vdir else f"{base}/{target}"
    r = s.get(seg_url, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"segment {r.status_code}")
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tf:
        tf.write(r.content)
        ts_path = tf.name
    try:
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", ts_path, "-ss", "1", "-frames:v", "1", out_png],
                       check=True)
    finally:
        Path(ts_path).unlink(missing_ok=True)


def fetch_frame_from_video(video_path, t_sec, out_png):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{t_sec:.1f}", "-i", video_path,
                    "-frames:v", "1", out_png], check=True)


def fit_title_font(draw, title, font_path, max_width, start_px=92, min_px=48):
    size = start_px
    while size > min_px:
        font = ImageFont.truetype(font_path, size)
        if font.getlength(title) <= max_width:
            return font, title
        size -= 4
    font = ImageFont.truetype(font_path, min_px)
    while title and font.getlength(title + "…") > max_width:
        title = title[:-1]
    return font, (title + "…") if title else ""


def compose(frame_png, title, date_slash, font_path, out_jpg):
    im = Image.open(frame_png).convert("RGB").resize((W, H), Image.LANCZOS)
    draw = ImageDraw.Draw(im, "RGBA")

    # タイトル帯 (上部)
    font_t, title_fit = fit_title_font(draw, title, font_path, W - 80)
    band_h = font_t.size + 44
    draw.rectangle([0, MARGIN, W, MARGIN + band_h], fill=(0, 0, 0, 150))
    draw.text((40, MARGIN + band_h // 2), title_fit, font=font_t, anchor="lm",
              fill=(255, 255, 255), stroke_width=6, stroke_fill=(0, 0, 0))

    # 配信日 (右下)
    font_d = ImageFont.truetype(font_path, 56)
    tw = font_d.getlength(date_slash)
    pad = 18
    x1 = W - tw - pad * 2 - MARGIN
    y1 = H - 56 - pad * 2 - MARGIN
    draw.rounded_rectangle([x1, y1, W - MARGIN, H - MARGIN], radius=12,
                           fill=(0, 0, 0, 170))
    draw.text((x1 + pad, (y1 + H - MARGIN) // 2), date_slash, font=font_d,
              anchor="lm", fill=(255, 255, 255))

    im.save(out_jpg, quality=90)
    # サムネAPIの上限2MBを保険で守る
    if Path(out_jpg).stat().st_size > 2_000_000:
        im.save(out_jpg, quality=75)
    print(f"thumbnail: {out_jpg}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", default="out/meta.json")
    ap.add_argument("--chat", default="out/chat.jsonl")
    ap.add_argument("--font", default="fonts/BIZUDPGothic-Regular.ttf")
    ap.add_argument("--out", default="thumb.jpg")
    ap.add_argument("--fallback-video", default="")
    a = ap.parse_args()
    meta = json.loads(Path(a.meta).read_text(encoding="utf-8"))
    peak = find_hype_peak(a.chat, meta["duration_s"])
    print(f"hype peak: {peak:.0f}s", file=sys.stderr)

    frame = "thumb_frame.png"
    try:
        if not meta.get("source"):
            raise RuntimeError("no source url")
        fetch_frame_from_source(meta["source"], peak, frame)
    except Exception as e:
        print(f"source frame failed ({e})", file=sys.stderr)
        if a.fallback_video and Path(a.fallback_video).exists():
            fetch_frame_from_video(a.fallback_video, peak, frame)
        else:
            raise

    date_slash = str(meta["date"]).replace("-", "/")
    compose(frame, meta["title"], date_slash, a.font, a.out)


if __name__ == "__main__":
    main()
