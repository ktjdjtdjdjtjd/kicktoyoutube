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


def find_file(emote_dir, eid):
    """蓄積ディレクトリから <id>.gif / <id>.png を探す。無ければ None。"""
    for ext in ("gif", "png"):
        p = Path(emote_dir) / f"{eid}.{ext}"
        if p.exists():
            return p
    return None


def to_png_bytes(raw):
    """PNG/GIF以外(webp等)のバイト列をPNGへ変換。"""
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def download_missing(ids, emote_dir, session=None):
    """未取得のエモートを原本のままDLして蓄積 (GIFはアニメ保持のため<id>.gif)。
    新規保存したPathのリストを返す。"""
    emote_dir = Path(emote_dir)
    emote_dir.mkdir(parents=True, exist_ok=True)
    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome")
    added = []
    for eid in sorted(ids):
        if find_file(emote_dir, eid):
            continue
        try:
            r = session.get(EMOTE_URL.format(id=eid), timeout=20)
            if r.status_code != 200:
                print(f"emote {eid}: HTTP {r.status_code} — skip", file=sys.stderr)
                continue
            raw = r.content
            if raw[:6] in (b"GIF87a", b"GIF89a"):
                dest = emote_dir / f"{eid}.gif"
                dest.write_bytes(raw)
            elif raw[:8] == b"\x89PNG\r\n\x1a\n":
                dest = emote_dir / f"{eid}.png"
                dest.write_bytes(raw)
            else:
                dest = emote_dir / f"{eid}.png"
                dest.write_bytes(to_png_bytes(raw))
            added.append(dest)
        except Exception as e:
            print(f"emote {eid}: {e} — skip", file=sys.stderr)
    total = len(list(Path(emote_dir).glob("*.png"))) + len(list(Path(emote_dir).glob("*.gif")))
    print(f"emotes: {len(added)} downloaded, dir total {total}", file=sys.stderr)
    return added
