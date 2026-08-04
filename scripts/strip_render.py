"""ダンマクレイヤーのストリップ描画 (テキスト+エモート画像+カラー絵文字をPILで事前合成)。

全メッセージ等速(speed px/s)にすることで「1レーン=1枚の横長画像」になり、
ffmpeg は overlay をレーン数ぶん置くだけで済む (libass不使用・エモートはフルカラー)。

- GIFエモート: 位相K枚のストリップ変種を作り、burn側でoverlayを時分割切替してアニメさせる
- 絵文字: BIZ UDPGothicに無いグリフは Noto Color Emoji (CBDT) でカラー描画
- 座標系は実動画の解像度に合わせる (scale = video_h / 1080)
- レーン割当は常に全メッセージ一括・同一パラメータで計算 (セグメント間の連続性)
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageSequence
from fontTools.ttLib import TTFont

import emotes as emotes_mod

TEXT_ALPHA = 165
EMOTE_ALPHA = 210
EMOJI_RENDER_PX = 109  # NotoColorEmoji(CBDT)はこのサイズ固定でしか描けない
JOINERS = {0x200D, 0xFE0F, 0xFE0E}


class Params:
    """1080p基準の定数を scale 倍した実ピクセル値。configのdanmakuセクションで上書き可。"""

    def __init__(self, scale=1.0, cfg=None):
        d = (cfg or {}).get("danmaku", {}) if cfg else {}
        self.scale = scale
        self.speed = float(d.get("speed", 150)) * scale          # px/s (1080p基準)
        self.n_lanes = 14
        self.lane_h = max(24, round(71 * scale))
        self.lane_top = [round((10 + i * 71) * scale) for i in range(self.n_lanes)]
        self.font_px = max(16, round(d.get("font_px", 60) * scale))
        self.emote_px = max(16, round(d.get("emote_px", 64) * scale))
        self.emote_pad = max(2, round(6 * scale))
        self.gap = 48 * scale
        self.stroke = max(1, round(3 * scale))
        self.max_msg_w = 2800 * scale
        self.left_margin = round(4096 * scale)
        self.right_pad = round(self.max_msg_w + 256)
        self.screen_w = round(1920 * scale)
        self.gif_phases = int(d.get("gif_phases", 4))
        self.gif_phase_fps = float(d.get("gif_phase_fps", 5))


def load_cmap(font_path):
    try:
        ft = TTFont(str(font_path), fontNumber=0, lazy=True)
        cmap = set(ft.getBestCmap().keys())
        ft.close()
        return cmap
    except Exception as e:
        print(f"cmap load failed for {font_path}: {e}", file=sys.stderr)
        return set()


class TextShaper:
    """本文フォント(BIZ)に無い文字をカラー絵文字フォントへ振り分けて描画する。"""

    def __init__(self, font_path, emoji_font_path, p):
        self.p = p
        self.font = ImageFont.truetype(str(font_path), p.font_px)
        self.main_cmap = load_cmap(font_path)
        self.emoji_font = None
        self.emoji_cmap = set()
        if emoji_font_path and Path(emoji_font_path).exists():
            try:
                self.emoji_font = ImageFont.truetype(str(emoji_font_path), EMOJI_RENDER_PX)
                self.emoji_cmap = load_cmap(emoji_font_path)
            except Exception as e:
                print(f"emoji font load failed: {e}", file=sys.stderr)
        self.emoji_scale = p.font_px / EMOJI_RENDER_PX
        self._emoji_cache = {}

    def split_runs(self, text):
        """[('t', s)|('j', s)] に分割。'j'=絵文字フォントで描く連続列。
        どちらのフォントにも無い文字は落とす (豆腐対策)。"""
        runs = []
        cur_kind = None
        cur = []
        for ch in text:
            cp = ord(ch)
            if cp in self.main_cmap and not (cp in self.emoji_cmap and cp > 0x2600):
                kind = "t"
            elif cp in self.emoji_cmap or cp in JOINERS:
                kind = "j"
            elif cp in self.main_cmap:
                kind = "t"
            else:
                continue  # どちらにも無い → 落とす
            if kind != cur_kind and cur:
                runs.append((cur_kind, "".join(cur)))
                cur = []
            cur_kind = kind
            cur.append(ch)
        if cur:
            runs.append((cur_kind, "".join(cur)))
        if not self.emoji_font:
            runs = [(k, s) for k, s in runs if k == "t"]
        return runs

    def run_width(self, kind, s):
        if kind == "t":
            return self.font.getlength(s)
        return self.emoji_font.getlength(s) * self.emoji_scale

    def render_emoji(self, s):
        """絵文字列 → font_px 高のRGBA画像 (キャッシュ)。"""
        if s in self._emoji_cache:
            return self._emoji_cache[s]
        w = max(1, int(self.emoji_font.getlength(s)) + 8)
        im = Image.new("RGBA", (w, EMOJI_RENDER_PX + 20), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        try:
            d.text((0, 10), s, font=self.emoji_font, embedded_color=True)
        except Exception:
            self._emoji_cache[s] = None
            return None
        sc = self.emoji_scale
        im = im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))),
                       Image.LANCZOS)
        a = im.getchannel("A").point(lambda v: v * EMOTE_ALPHA // 255)
        im.putalpha(a)
        self._emoji_cache[s] = im
        return im


class EmoteBank:
    """emotes/<id>.png|.gif を読み、位相フレーム列に正規化して供給。"""

    def __init__(self, emote_dir, p):
        self.dir = Path(emote_dir)
        self.p = p
        self._cache = {}

    def _normalize(self, im):
        px = self.p.emote_px
        im = im.convert("RGBA")
        im.thumbnail((px, px), Image.LANCZOS)
        canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        canvas.paste(im, ((px - im.width) // 2, (px - im.height) // 2))
        a = canvas.getchannel("A").point(lambda v: v * EMOTE_ALPHA // 255)
        canvas.putalpha(a)
        return canvas

    def get(self, eid):
        """{'frames': [img×K or ×1], 'animated': bool} or None"""
        if eid in self._cache:
            return self._cache[eid]
        path_gif = self.dir / f"{eid}.gif"
        path_png = self.dir / f"{eid}.png"
        result = None
        try:
            if path_gif.exists():
                src = Image.open(path_gif)
                raw_frames = []
                durations = []
                for fr in ImageSequence.Iterator(src):
                    raw_frames.append(fr.convert("RGBA"))
                    durations.append(max(20, fr.info.get("duration", 100)))
                if len(raw_frames) == 1:
                    result = {"frames": [self._normalize(raw_frames[0])], "animated": False}
                else:
                    total = sum(durations)
                    cum = []
                    acc = 0
                    for du in durations:
                        cum.append((acc, acc + du))
                        acc += du
                    frames = []
                    for k in range(self.p.gif_phases):
                        t_ms = (k / self.p.gif_phase_fps * 1000) % total
                        idx = next(i for i, (s, e) in enumerate(cum) if s <= t_ms < e)
                        frames.append(self._normalize(raw_frames[idx]))
                    result = {"frames": frames, "animated": True}
            elif path_png.exists():
                result = {"frames": [self._normalize(Image.open(path_png))], "animated": False}
        except Exception as e:
            print(f"emote {eid}: load failed ({e})", file=sys.stderr)
            result = None
        self._cache[eid] = result
        return result


def measure_tokens(tokens, shaper, p):
    """トークン列の描画幅。max_msg_w 超過はテキストを切り詰める。
    戻り: (norm_tokens, width)  norm_tokens: [('emote', id)|('t', s)|('j', s)]"""
    out = []
    w = 0.0
    for kind, val in tokens:
        if kind == "emote":
            if w + p.emote_px + p.emote_pad > p.max_msg_w:
                break
            out.append(("emote", val))
            w += p.emote_px + p.emote_pad
            continue
        for rkind, s in shaper.split_runs(val):
            rw = shaper.run_width(rkind, s)
            if w + rw <= p.max_msg_w:
                out.append((rkind, s))
                w += rw
            elif rkind == "t":
                lo, hi = 0, len(s)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if w + shaper.run_width("t", s[:mid] + "…") <= p.max_msg_w:
                        lo = mid
                    else:
                        hi = mid - 1
                if lo > 0:
                    out.append(("t", s[:lo] + "…"))
                    w += shaper.run_width("t", s[:lo] + "…")
                return out, w
            else:
                return out, w
    return out, w


def layout(messages, shaper, p):
    """全メッセージへレーンを割り当てる。戻り: [(rel, lane, tokens, width)]"""
    lane_free = [float("-inf")] * p.n_lanes
    placed = []
    for rel, content in messages:
        tokens, width = measure_tokens(emotes_mod.tokenize(content), shaper, p)
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


def render_strips(placed, seg_start, seg_end, shaper, bank, out_dir, p):
    """セグメント窓に掛かるメッセージをレーン別ストリップPNGへ描画。
    GIFエモートを含むレーンは位相K枚の変種を作る。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_dur = seg_end - seg_start
    width = int(p.left_margin + seg_dur * p.speed + p.screen_w + p.right_pad)
    lookback = (p.screen_w + p.max_msg_w) / p.speed

    lanes = {}
    for rel, lane, tokens, w in placed:
        if rel < seg_start - lookback or rel >= seg_end:
            continue
        lanes.setdefault(lane, []).append((rel, tokens, w))

    manifest = {"speed": p.speed, "left_margin": p.left_margin,
                "screen_w": p.screen_w, "gif_phase_fps": p.gif_phase_fps, "strips": []}

    for lane in sorted(lanes):
        animated = False
        for rel, tokens, w in lanes[lane]:
            for kind, val in tokens:
                if kind == "emote":
                    e = bank.get(val)
                    if e and e["animated"]:
                        animated = True
        n_variants = p.gif_phases if animated else 1
        files = []
        for phase in range(n_variants):
            strip = Image.new("RGBA", (width, p.lane_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(strip)
            y_mid = p.lane_h // 2
            for rel, tokens, w in lanes[lane]:
                x = p.left_margin + (rel - seg_start) * p.speed
                if x + w < 0 or x > width:
                    continue
                cx = x
                for kind, val in tokens:
                    if kind == "emote":
                        e = bank.get(val)
                        if e is not None:
                            fr = e["frames"][phase % len(e["frames"])]
                            strip.alpha_composite(
                                fr, (int(cx + p.emote_pad / 2), y_mid - p.emote_px // 2))
                        cx += p.emote_px + p.emote_pad
                    elif kind == "j":
                        im = shaper.render_emoji(val)
                        if im is not None:
                            strip.alpha_composite(im, (int(cx), y_mid - im.height // 2))
                        cx += shaper.run_width("j", val)
                    else:
                        draw.text((cx, y_mid), val, font=shaper.font, anchor="lm",
                                  fill=(255, 255, 255, TEXT_ALPHA),
                                  stroke_width=p.stroke, stroke_fill=(0, 0, 0, TEXT_ALPHA))
                        cx += shaper.font.getlength(val)
            fname = f"strip_{lane:02d}_p{phase}.png"
            strip.save(out_dir / fname)
            files.append(fname)
        manifest["strips"].append({"lane": lane, "y": p.lane_top[lane], "files": files})
        print(f"lane {lane}: {len(lanes[lane])} msgs, variants={n_variants}", file=sys.stderr)

    (out_dir / "strips.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def build_for_segment(chat_jsonl, seg_start, seg_end, font_path, emote_dir, out_dir,
                      scale=1.0, cfg=None, emoji_font_path=None):
    p = Params(scale, cfg)
    shaper = TextShaper(font_path, emoji_font_path, p)
    bank = EmoteBank(emote_dir, p)
    messages = []
    with open(chat_jsonl, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            messages.append((d["rel"], d["content"]))
    messages.sort(key=lambda x: x[0])
    placed = layout(messages, shaper, p)
    return render_strips(placed, seg_start, seg_end, shaper, bank, out_dir, p)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("chat_jsonl")
    ap.add_argument("seg_start", type=float)
    ap.add_argument("seg_end", type=float)
    ap.add_argument("--font", default="fonts/BIZUDPGothic-Regular.ttf")
    ap.add_argument("--emoji-font", default="fonts/NotoColorEmoji.ttf")
    ap.add_argument("--emotes", default="out/emotes")
    ap.add_argument("--out", default="strips")
    ap.add_argument("--scale", type=float, default=1.0)
    a = ap.parse_args()
    build_for_segment(a.chat_jsonl, a.seg_start, a.seg_end, a.font, a.emotes, a.out,
                      scale=a.scale, emoji_font_path=a.emoji_font)
