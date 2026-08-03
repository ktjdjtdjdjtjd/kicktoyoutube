"""Kickエモート画像の取得・蓄積・正規化。

- メッセージ中の [emote:<id>:<name>] マーカーを解析
- 画像は emotes/<id>.png に蓄積 (GIFは先頭フレームをPNG化・64x64正方に正規化)
- リポジトリにコミットして永続蓄積する運用 (ディスク掃除で消えない)
"""
import io
import re
import sys
from pathlib import Path

EMOTE_RE = re.compile(r"\[emote:(\d+):([^\]]*)\]")
EMOTE_URL = "https://files.kick.com/emotes/{id}/fullsize"
EMOTE_PX = 64


def tokenize(content):
    """'あはは[emote:123:Sadge]ｗ' -> [('text','あはは'),('emote','123'),('text','ｗ')]"""
    tokens = []
    pos = 0
    for m in EMOTE_RE.finditer(content):
        if m.start() > pos:
            tokens.append(("text", content[pos:m.start()]))
        tokens.append(("emote", m.group(1)))
        pos = m.end()
    if pos < len(content):
        tokens.append(("text", content[pos:]))
    return tokens


def collect_ids(messages):
    ids = set()
    for _, content in messages:
        for m in EMOTE_RE.finditer(content):
            ids.add(m.group(1))
    return ids


def normalize_png(raw, px=EMOTE_PX):
    """PNG/GIFバイト列 -> px×px 透過PNGバイト列 (GIFは先頭フレーム)。"""
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    im.seek(0)
    im = im.convert("RGBA")
    im.thumbnail((px, px), Image.LANCZOS)
    canvas = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    canvas.paste(im, ((px - im.width) // 2, (px - im.height) // 2))
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def download_missing(ids, emote_dir, session=None):
    """未取得のエモートをDLして emote_dir に保存。新規保存したPathのリストを返す。"""
    emote_dir = Path(emote_dir)
    emote_dir.mkdir(parents=True, exist_ok=True)
    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome")
    added = []
    for eid in sorted(ids):
        dest = emote_dir / f"{eid}.png"
        if dest.exists():
            continue
        try:
            r = session.get(EMOTE_URL.format(id=eid), timeout=20)
            if r.status_code != 200:
                print(f"emote {eid}: HTTP {r.status_code} — skip", file=sys.stderr)
                continue
            dest.write_bytes(normalize_png(r.content))
            added.append(dest)
        except Exception as e:
            print(f"emote {eid}: {e} — skip", file=sys.stderr)
    print(f"emotes: {len(added)} downloaded, dir total {len(list(emote_dir.glob('*.png')))}",
          file=sys.stderr)
    return added
