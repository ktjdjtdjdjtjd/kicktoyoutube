"""サムネイル自動生成。

チャット密度が最大の60秒窓を「盛り上がりピーク」とみなし、その時刻のフレームを
Kickのsource m3u8から直接取得(クリーンな1セグメントのみDL)。
タイトル帯(上部)+配信日(右下)を合成して1280x720のJPEGを出力する。

    python thumbnail.py --meta out/meta.json --chat out/chat.jsonl \
        --font fonts/BIZUDPGothic-Regular.ttf --out thumb.jpg [--fallback-video final.mp4]
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import jp_wrap
from strip_render import EMOJI_RENDER_PX, TextShaper

W, H = 1280, 720
RED = (0xE6, 0x00, 0x12)
MARGIN = 24


class _ShaperParams:
    """TextShaper が参照する最小パラメータ (サムネはダンマク設定と無関係)。"""

    def __init__(self, font_px):
        self.font_px = font_px


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


def title_width(shaper, title):
    return sum(shaper.run_width(k, s) for k, s in shaper.split_runs(title))


def fit_title(title, font_path, emoji_font_path, max_width, start_px=92, min_px=48):
    """(shaper, 表示タイトル) を返す。絵文字混在幅で縮小→末尾…切り詰め。"""
    ref = TextShaper(font_path, emoji_font_path, _ShaperParams(100))
    w100 = title_width(ref, title)
    size = int(100 * max_width / w100) if w100 > 0 else start_px
    size = max(min_px, min(start_px, size))
    limit100 = max_width * 100 / size  # 参照サイズ(100px)換算の幅上限
    if title_width(ref, title) > limit100:
        while title and title_width(ref, title + "…") > limit100:
            title = title[:-1]
        title = (title + "…") if title else ""
    shaper = TextShaper(font_path, emoji_font_path, _ShaperParams(size))
    return shaper, title


def render_emoji_opaque(shaper, s):
    """絵文字列 → タイトル用の不透明RGBA画像 (ダンマク用の減光は掛けない)。"""
    if not shaper.emoji_font:
        return None
    w = max(1, int(shaper.emoji_font.getlength(s)) + 8)
    im = Image.new("RGBA", (w, EMOJI_RENDER_PX + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    try:
        d.text((0, 10), s, font=shaper.emoji_font, embedded_color=True)
    except Exception:
        return None
    sc = shaper.emoji_scale
    return im.resize((max(1, int(im.width * sc)), max(1, int(im.height * sc))),
                     Image.LANCZOS)


def draw_title_runs(im, draw, shaper, title, x, cy, stroke_w):
    """BIZで描けない絵文字はNoto Color Emojiのビットマップを合成して描く。"""
    for kind, s in shaper.split_runs(title):
        if kind == "t":
            draw.text((x, cy), s, font=shaper.font, anchor="lm",
                      fill=(255, 255, 255), stroke_width=stroke_w,
                      stroke_fill=(0, 0, 0))
            x += shaper.font.getlength(s)
        else:
            em = render_emoji_opaque(shaper, s)
            if em:
                im.paste(em, (int(x), int(cy - em.height / 2)), em)
            x += shaper.run_width(kind, s)


def _split_two_lines(title):
    """タイトルを2行に分割する。日本語として変な位置(句点の前・語の途中)で折らない。
    1) 空白・文末記号(。！？…や「・・・」。連なりは末尾まで前行)のうち最も均等な位置
    2) それが無ければ jp_wrap の切れ目コスト + 行長の偏り が最小の位置
    2026-09-08 ユーザー指摘(「自首しにいきました|。全てを話します！」で割れていた)。"""
    import re
    title = title.strip()
    n = len(title)
    if n < 2:
        return title, ""

    def _ok(i):   # 両行が空でなく、長い方が全体の8割以下
        return 0 < i < n and max(i, n - i) <= n * 0.8

    cands = []
    for m in re.finditer(r"[ 　]+|[。！？!?．…‥]+|・{2,}", title):
        for i in (m.start(), m.end()):
            if _ok(i) and title[i] not in jp_wrap.NO_START:
                cands.append(i)
    if cands:
        i = min(cands, key=lambda i: abs(2 * i - n))
        return title[:i].strip(), title[i:].strip()

    def _cost(i):
        if not jp_wrap._can_break(title, i):
            return None
        pen = jp_wrap._break_penalty(title, i)
        if title[i - 1] in "、，":
            pen = 1.5
        elif title[i - 1] in "」』）】" or title[i] in "「『（【":
            pen = 1.0
        return pen + abs(2 * i - n)

    scored = [(c, i) for i in range(1, n) if (c := _cost(i)) is not None]
    i = min(scored)[1] if scored else n // 2
    a, b = title[:i].strip(), title[i:].strip()
    return (a, b) if a and b else (title[:n // 2], title[n // 2:])


def _fit_multiline(lines, font_path, emoji_font_path, max_width, start_px=120, min_px=48, step=2):
    """複数行すべてがmax_widthに収まる最大pxのshaperを (px, shaper) で返す。"""
    px = start_px
    shaper = TextShaper(font_path, emoji_font_path, _ShaperParams(px))
    while px > min_px:
        shaper = TextShaper(font_path, emoji_font_path, _ShaperParams(px))
        if all(title_width(shaper, ln) <= max_width for ln in lines if ln):
            return px, shaper
        px -= step
    return min_px, shaper


def _truncate_to_width(shaper, text, max_width):
    """末尾を「…」で切り詰めてmax_widthに収める (fit_titleの切り詰めと同じ考え方)。"""
    if title_width(shaper, text) <= max_width:
        return text
    while text and title_width(shaper, text + "…") > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def _fit_title_p10s(title, font_path, emoji_font_path, max_width):
    """中央帯タイトルのフィッティング連鎖。
    150px単行 -> 90px単行 -> 2行(120px上限, 48pxまで縮小) -> それでも収まらなければ2行目末尾を省略。
    戻り値: (lines, shaper)。"""
    for px in (150, 90):
        shaper = TextShaper(font_path, emoji_font_path, _ShaperParams(px))
        if title_width(shaper, title) <= max_width:
            return [title], shaper

    line1, line2 = _split_two_lines(title)
    px, shaper = _fit_multiline([line1, line2], font_path, emoji_font_path, max_width,
                                start_px=120, min_px=48)
    if title_width(shaper, line2) > max_width:
        line2 = _truncate_to_width(shaper, line2, max_width)
    if title_width(shaper, line1) > max_width:
        line1 = _truncate_to_width(shaper, line1, max_width)
    return [line1, line2], shaper


def _text_block_size(draw, text, font, stroke_width):
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _label(im, x, y, text, font, fg, bg, pad=(22, 10), alpha=255, align="left"):
    """角アリの矩形ラベルを描く(RGBA レイヤ合成)。align="right" なら x を右端として扱う。戻り値は高さ。"""
    bb = ImageDraw.Draw(im).textbbox((0, 0), text, font=font)
    w = bb[2] - bb[0] + pad[0] * 2
    h = bb[3] - bb[1] + pad[1] * 2
    x0 = x - w if align == "right" else x
    lay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(lay).rectangle((x0, y, x0 + w, y + h), fill=bg + (alpha,))
    im.alpha_composite(lay)
    ImageDraw.Draw(im).text((x0 + pad[0] - bb[0], y + pad[1] - bb[1]), text, font=font, fill=fg)
    return h


def draw_channel_tag(im, font_path, name, sub="アーカイブ(コメあり)"):
    """右上=チャンネル名(赤地白字・大)、左上=アーカイブ(コメあり)(白地赤字・小)。角アリ。
    font_path はタグ専用フォント(タイトルの keifont とは別に指定できる)。"""
    m, y = 40, 36
    if name:
        _label(im, im.width - m, y, name, ImageFont.truetype(font_path, 44), (255, 255, 255), RED, align="right")
    if sub:
        _label(im, m, y, sub, ImageFont.truetype(font_path, 28), RED, (255, 255, 255), pad=(16, 8), alpha=230)


def channel_display_name(slug, config_path=None):
    """config.json の title_template 先頭の【…】をチャンネル表示名として返す。無ければ ''。"""
    try:
        cfg_path = Path(config_path) if config_path else Path(__file__).resolve().parent.parent / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        tpl = (cfg.get("channel_settings", {}).get(slug or "", {}).get("title_template")
               or cfg.get("title_template", ""))
        m = re.match(r"【(.+?)】", tpl)
        return m.group(1) if m else ""
    except Exception as e:
        print(f"[thumbnail] channel name lookup failed: {e}", file=sys.stderr)
        return ""


def compose(frame_png, title, date_slash, font_path, out_jpg, emoji_font_path="",
            band_alpha=150, band_extra=0, title_font_path="",
            tag_name="", tag_sub="アーカイブ(コメあり)", tag_font_path=""):
    """P10s: 縦中央に赤半透明帯+白文字(縁なし)のタイトル、配信日は左下(白文字+黒縁)。

    band_alpha=255 は既存サムネの上から帯だけ塗り直すモード (rethumbの豆腐修理用、
    実質不透明になる)。band_extra は旧レイアウト(上部帯)で帯の高さを追加調整するための
    引数だったが、新レイアウトは帯の高さをテキストブロックから直接算出するため未使用
    (rethumb.py 側の既存呼び出しとの互換のため引数だけ残す)。
    """
    tfont_path = title_font_path or str(
        Path(__file__).resolve().parent.parent / "fonts" / "keifont.ttf")
    if not Path(tfont_path).exists():
        print(f"[thumbnail] title font not found: {tfont_path} -> fallback {font_path}",
              file=sys.stderr)
        tfont_path = font_path
    # タグ(チャンネル名/アーカイブ)専用フォント = 源暎ポップル。無ければタイトルと同じ keifont
    gfont_path = tag_font_path or str(
        Path(__file__).resolve().parent.parent / "fonts" / "GenEiPOPle-Bk.ttf")
    if not Path(gfont_path).exists():
        print(f"[thumbnail] tag font not found: {gfont_path} -> fallback {tfont_path}", file=sys.stderr)
        gfont_path = tfont_path

    im = Image.open(frame_png).convert("RGB").resize((W, H), Image.LANCZOS).convert("RGBA")

    max_w = W - 120
    lines, shaper = _fit_title_p10s(title, tfont_path, emoji_font_path, max_w)
    ascent, descent = shaper.font.getmetrics()
    line_h = ascent + descent
    block_h = line_h * len(lines)
    band_pad = 30
    band_h = block_h + band_pad * 2

    # 赤半透明帯は別レイヤーに描いてalpha_compositeで合成する
    # (ImageDraw.rectangleでRGBAを直接ベース画像に塗ると下地との混色にならず不透明になるため)
    band_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    band_draw = ImageDraw.Draw(band_layer)
    band_y0 = H / 2 - band_h / 2
    band_y1 = H / 2 + band_h / 2
    band_draw.rectangle([0, band_y0, W, band_y1], fill=RED + (band_alpha,))
    im = Image.alpha_composite(im, band_layer)

    draw = ImageDraw.Draw(im, "RGBA")
    top = H / 2 - block_h / 2
    for i, line in enumerate(lines):
        if not line:
            continue
        cy = top + line_h * i + line_h / 2
        w = title_width(shaper, line)
        x = (W - w) / 2
        draw_title_runs(im, draw, shaper, line, x, cy, stroke_w=0)

    # 右上=チャンネル名 / 左上=アーカイブ(コメあり)
    if tag_name or tag_sub:
        draw_channel_tag(im, gfont_path, tag_name, tag_sub)
        draw = ImageDraw.Draw(im, "RGBA")

    # 配信日 (左下、白文字+黒縁4px)
    font_d = ImageFont.truetype(tfont_path, 48)
    _, dh = _text_block_size(draw, date_slash, font_d, 4)
    dx, dy = 40, H - 40 - dh
    draw.text((dx, dy), date_slash, font=font_d, fill=(255, 255, 255),
              stroke_width=4, stroke_fill=(0, 0, 0))

    im = im.convert("RGB")
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
    ap.add_argument("--emoji-font", default="fonts/NotoColorEmoji.ttf")
    ap.add_argument("--out", default="thumb.jpg")
    ap.add_argument("--fallback-video", default="")
    ap.add_argument("--title-font", default="")
    ap.add_argument("--tag-name", default=None, help="右上タグのチャンネル名(省略時は config.json から自動、空文字で非表示)")
    ap.add_argument("--tag-sub", default="アーカイブ(コメあり)")
    ap.add_argument("--tag-font", default="", help="タグ専用フォント(省略時は fonts/GenEiPOPle-Bk.ttf)")
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
    tag_name = a.tag_name if a.tag_name is not None else channel_display_name(meta.get("slug"))
    compose(frame, meta["title"], date_slash, a.font, a.out,
            emoji_font_path=a.emoji_font, title_font_path=a.title_font,
            tag_name=tag_name, tag_sub=a.tag_sub, tag_font_path=a.tag_font)


if __name__ == "__main__":
    main()
