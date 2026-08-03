"""ダンマクレイヤーのストリップ描画 (テキスト+エモート画像をPILで事前合成)。

全メッセージ等速(SPEED px/s)にすることで「1レーン=1枚の横長画像」になり、
ffmpeg は overlay をレーン数ぶん置くだけで済む (libass不使用・エモートはフルカラー)。

座標系は実動画の解像度に合わせる (scale = video_h / 1080)。
セグメント間の連続性のため、レーン割当は常に全メッセージ一括で同一パラメータで計算する。
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import emotes as emotes_mod

TEXT_ALPHA = 165
EMOTE_ALPHA = 210


class Params:
    """1080p基準の定数を scale 倍した実ピクセル値。"""

    def __init__(self, scale=1.0):
        self.scale = scale
        self.speed = 150.0 * scale          # px/s
        self.n_lanes = 14
        self.lane_h = max(24, round(71 * scale))
        self.lane_top = [round((10 + i * 71) * scale) for i in range(self.n_lanes)]
        self.font_px = max(16, round(60 * scale))
        self.emote_px = max(16, round(64 * scale))
        self.emote_pad = max(2, round(6 * scale))
        self.gap = 48 * scale
        self.stroke = max(1, round(3 * scale))
        self.max_msg_w = 2800 * scale
        self.left_margin = round(4096 * scale)
        self.right_pad = round(self.max_msg_w + 256)
        self.screen_w = round(1920 * scale)


def measure_tokens(tokens, font, p):
    """トークン列の描画幅。max_msg_w 超過はテキストを切り詰める。戻り: (tokens, width)"""
    out = []
    w = 0.0
    for kind, val in tokens:
        if kind == "emote":
            if w + p.emote_px + p.emote_pad > p.max_msg_w:
                break
            out.append((kind, val))
            w += p.emote_px + p.emote_pad
        else:
            tw = font.getlength(val)
            if w + tw <= p.max_msg_w:
                out.append((kind, val))
                w += tw
            else:
                lo, hi = 0, len(val)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if w + font.getlength(val[:mid] + "…") <= p.max_msg_w:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo > 0:
                    out.append((kind, val[:lo] + "…"))
                    w += font.getlength(val[:lo] + "…")
                break
    return out, w


def layout(messages, font, p):
    """全メッセージへレーンを割り当てる。戻り: [(rel, lane, tokens, width)]"""
    lane_free = [float("-inf")] * p.n_lanes
    placed = []
    for rel, content in messages:
        tokens = emotes_mod.tokenize(content)
        tokens, width = measure_tokens(tokens, font, p)
        if not tokens or width <= 0:
            continue
        x = rel * p.speed
        lane = None
        for i in range(p.n_lanes):
            if lane_free[i] <= x:
                lane = i
                break
        if lane is None:
            lane = min(range(p.n_lanes), key=lambda i: lane_free[i])
        lane_free[lane] = x + width + p.gap
        placed.append((rel, lane, tokens, width))
    return placed


def render_strips(placed, seg_start, seg_end, font, emote_dir, out_dir, p):
    """セグメント窓に掛かるメッセージをレーン別ストリップPNGへ描画。

    ストリップ座標: x_strip = left_margin + (rel - seg_start) * speed
    画面座標(セグメント内時刻t): x_screen = screen_w - left_margin - t*speed + x_strip
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    emote_dir = Path(emote_dir)
    seg_dur = seg_end - seg_start
    width = int(p.left_margin + seg_dur * p.speed + p.screen_w + p.right_pad)
    lookback = (p.screen_w + p.max_msg_w) / p.speed

    lanes = {}
    for rel, lane, tokens, w in placed:
        if rel < seg_start - lookback or rel >= seg_end:
            continue
        lanes.setdefault(lane, []).append((rel, tokens, w))

    emote_cache = {}

    def get_emote(eid):
        if eid not in emote_cache:
            path = emote_dir / f"{eid}.png"
            if path.exists():
                im = Image.open(path).convert("RGBA")
                if im.width != p.emote_px:
                    im = im.resize((p.emote_px, p.emote_px), Image.LANCZOS)
                a = im.getchannel("A").point(lambda v: v * EMOTE_ALPHA // 255)
                im.putalpha(a)
                emote_cache[eid] = im
            else:
                emote_cache[eid] = None
        return emote_cache[eid]

    manifest = {"speed": p.speed, "left_margin": p.left_margin,
                "screen_w": p.screen_w, "strips": []}
    for lane in sorted(lanes):
        strip = Image.new("RGBA", (width, p.lane_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(strip)
        y_mid = p.lane_h // 2
        n_drawn = 0
        for rel, tokens, w in lanes[lane]:
            x = p.left_margin + (rel - seg_start) * p.speed
            if x + w < 0 or x > width:
                continue
            cx = x
            for kind, val in tokens:
                if kind == "emote":
                    im = get_emote(val)
                    if im is not None:
                        strip.alpha_composite(
                            im, (int(cx + p.emote_pad / 2), y_mid - p.emote_px // 2))
                    cx += p.emote_px + p.emote_pad
                else:
                    draw.text((cx, y_mid), val, font=font, anchor="lm",
                              fill=(255, 255, 255, TEXT_ALPHA),
                              stroke_width=p.stroke, stroke_fill=(0, 0, 0, TEXT_ALPHA))
                    cx += font.getlength(val)
            n_drawn += 1
        fname = f"strip_{lane:02d}.png"
        strip.save(out_dir / fname)
        manifest["strips"].append({"lane": lane, "y": p.lane_top[lane], "file": fname})
        print(f"lane {lane}: {n_drawn} msgs -> {fname} ({width}px)", file=sys.stderr)

    (out_dir / "strips.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def build_for_segment(chat_jsonl, seg_start, seg_end, font_path, emote_dir, out_dir,
                      scale=1.0):
    p = Params(scale)
    font = ImageFont.truetype(str(font_path), p.font_px)
    messages = []
    with open(chat_jsonl, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            messages.append((d["rel"], d["content"]))
    messages.sort(key=lambda x: x[0])
    placed = layout(messages, font, p)
    return render_strips(placed, seg_start, seg_end, font, emote_dir, out_dir, p)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_jsonl")
    ap.add_argument("seg_start", type=float)
    ap.add_argument("seg_end", type=float)
    ap.add_argument("--font", default="fonts/BIZUDPGothic-Regular.ttf")
    ap.add_argument("--emotes", default="out/emotes")
    ap.add_argument("--out", default="strips")
    ap.add_argument("--scale", type=float, default=1.0)
    a = ap.parse_args()
    build_for_segment(a.chat_jsonl, a.seg_start, a.seg_end, a.font, a.emotes, a.out,
                      scale=a.scale)
