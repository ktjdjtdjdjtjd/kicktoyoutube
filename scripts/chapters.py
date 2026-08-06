"""投稿済み動画のタイムスタンプ(チャプター)自動生成→説明欄更新。

3時間おきのワークフローから呼ばれ、「status=done かつ chapters未処理」の動画を
古い順に1本処理する:
  1. Kickのsourceから低画質でDL → faster-whisper(CPU)で文字起こし
  2. Gemini APIへ実測タイムスタンプ付き文字起こしを渡し、事実ベースの章立てを生成
     (時刻は文字起こし由来のみ・創作禁止のプロンプト+構造検証の二重ガード)
  3. YouTube説明欄の「タイムスタンプ▽」節を更新 (要 youtube.force-ssl トークン)

    python chapters.py [--config config.json] [--uuid <特定動画>] [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import kick_api
from repo_state import STATE_DIR, commit_paths

CHAPTER_RE = re.compile(r"^(\d{1,2}:)?\d{1,2}:\d{2}\s+\S.*$")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent?key={key}")

PROMPT = """以下はライブ配信の文字起こしです。各行の先頭に実測のタイムスタンプが付いています。
この配信のYouTube用チャプター(タイムスタンプ)を作成してください。

## 出力形式(これ以外は一切出力しない)
0:00 配信開始
H:MM:SS 見出し
H:MM:SS 見出し

## ルール
- 見出しは10文字前後の体言止め。飾り言葉・煽り・絵文字は禁止
- 文字起こしに実際に出てくる出来事・話題だけを書く。推測・解釈・創作は禁止
- 話題が明確に切り替わった時だけ区切る(1時間あたり3〜6個)
- 固有名詞は文字起こしに登場する場合のみ。不確かなら書かない
- 内容が判断できない区間は「雑談」「移動」など中立的な語だけにする
- タイムスタンプは必ず文字起こしの行の時刻から選ぶ(時刻を創作しない)

## 文字起こし
{transcript}
"""


def hms(seconds):
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def parse_ts(t):
    """H:MM:SS / M:SS を秒へ。分・秒が60以上の壊れた表記は None。"""
    parts = [int(p) for p in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    if m > 59 or s > 59:
        return None
    return h * 3600 + m * 60 + s


def pick_candidate(only_uuid=None):
    cands = []
    for p in STATE_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("status") != "done" or not d.get("yt_url"):
            continue
        if d.get("chapters"):
            continue
        if not d.get("slug"):
            continue  # 旧手動投入分はメタ不足のため対象外
        if only_uuid and d.get("uuid") != only_uuid:
            continue
        cands.append((d.get("start_time") or "", p, d))
    cands.sort()
    return (cands[0][1], cands[0][2]) if cands else (None, None)


def download_audio(source_url, dest):
    subprocess.run(["yt-dlp", "--impersonate", "chrome",
                    "-f", "wv*+ba/w/b", "--no-part",
                    "--retries", "20", "--fragment-retries", "50",
                    "--concurrent-fragments", "4",
                    "--merge-output-format", "mp4",
                    "-o", dest, source_url], check=True)


def transcribe(path, model_size="small"):
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(path, language="ja", beam_size=1,
                                      vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 1500})
    lines = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            lines.append((seg.start, text))
    print(f"transcribed: {len(lines)} lines, duration={info.duration:.0f}s",
          file=sys.stderr)
    return lines, info.duration


MODEL_FALLBACKS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash",
                   "gemini-flash-latest"]


def bucketize(lines, bucket=60, max_chars=120):
    """文字起こしを bucket 秒単位に統合し、各行 max_chars に切り詰める。
    無料枠のトークン上限対策 (章検出にはこの粒度で十分)。"""
    out = []
    cur_start = None
    cur_text = []
    for t, txt in lines:
        b = int(t // bucket) * bucket
        if cur_start is None or b != cur_start:
            if cur_start is not None and cur_text:
                out.append((cur_start, " ".join(cur_text)[:max_chars]))
            cur_start = b
            cur_text = []
        cur_text.append(txt)
    if cur_start is not None and cur_text:
        out.append((cur_start, " ".join(cur_text)[:max_chars]))
    return out


def gemini_chapters(lines, api_key, model):
    compact = bucketize(lines)
    transcript = "\n".join(f"[{hms(t)}] {txt}" for t, txt in compact)
    print(f"transcript for gemini: {len(compact)} lines, {len(transcript)} chars",
          file=sys.stderr)
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT.format(transcript=transcript)}]}],
        "generationConfig": {"temperature": 0.2},
    }).encode()
    models = [model] + [m for m in MODEL_FALLBACKS if m != model]
    last_err = None
    for m in models:
        for attempt in range(1, 4):
            req = urllib.request.Request(
                GEMINI_URL.format(model=m, key=api_key), data=body,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    resp = json.loads(r.read())
                print(f"gemini model: {m}", file=sys.stderr)
                return resp["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                print(f"gemini {m}: HTTP {e.code} (attempt {attempt})", file=sys.stderr)
                last_err = e
                if e.code == 429 and attempt < 3:
                    import time
                    time.sleep(75)
                    continue
                break  # 404/400等 → 次のモデル候補へ
    raise last_err


def validate_chapters(raw, duration_s):
    """出力を検証・正規化。[(sec, label)] を返す。"""
    out = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-• ").strip()
        if not CHAPTER_RE.match(line):
            continue
        ts, label = line.split(None, 1)
        sec = parse_ts(ts)
        label = label.strip()
        if sec is None or sec > duration_s or not label or len(label) > 40:
            continue
        out.append((sec, label))
    # 昇順・最小間隔60秒・先頭0:00保証 (YouTube要件: 3個以上/各10秒以上)
    out.sort()
    cleaned = []
    for sec, label in out:
        if cleaned and sec - cleaned[-1][0] < 60:
            continue
        cleaned.append((sec, label))
    if not cleaned or cleaned[0][0] > 0:
        cleaned.insert(0, (0, "配信開始"))
    return cleaned


def inject_description(desc, chapters):
    block = "\n".join(f"{hms(s)} {label}" for s, label in chapters)
    head_marker = "タイムスタンプ▽"
    tail_marker = "元配信▽"
    if head_marker in desc and tail_marker in desc:
        head, rest = desc.split(head_marker, 1)
        _, tail = rest.split(tail_marker, 1)
        return f"{head}{head_marker}\n{block}\n\n{tail_marker}{tail}"
    return f"{desc}\n\n{head_marker}\n{block}"


def update_youtube_description(video_id, chapters):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    info = json.loads(os.environ["YT_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(
        info, ["https://www.googleapis.com/auth/youtube.force-ssl"])
    if not creds.valid:
        creds.refresh(Request())
    youtube = build("youtube", "v3", credentials=creds)
    items = youtube.videos().list(part="snippet", id=video_id).execute().get("items", [])
    if not items:
        raise RuntimeError(f"video not found: {video_id}")
    snippet = items[0]["snippet"]
    snippet["description"] = inject_description(snippet.get("description", ""), chapters)
    youtube.videos().update(part="snippet",
                            body={"id": video_id, "snippet": snippet}).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--uuid", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)
    ccfg = cfg.get("chapters", {})
    if not ccfg.get("enabled", True):
        print("chapters disabled")
        return
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("GEMINI_API_KEY not set — skip")
        return

    path, st = pick_candidate(a.uuid or None)
    if not st:
        print("no candidate")
        return
    print(f"target: {st.get('title')} {st['yt_url']}", file=sys.stderr)

    def mark(result, extra=None):
        st["chapters"] = {"result": result,
                          "at": datetime.now(timezone.utc).isoformat(), **(extra or {})}
        path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        commit_paths([path], f"state: chapters {result} ({st.get('uuid', '')[:8]})",
                     fatal=False)

    try:
        meta = None
        from plan import resolve_meta
        meta = resolve_meta(st["slug"], st["uuid"])
        if not meta.get("source"):
            raise RuntimeError("source unavailable (VOD expired?)")
    except Exception as e:
        print(f"meta failed: {e}", file=sys.stderr)
        mark("skipped-no-source")
        return

    download_audio(meta["source"], "low.mp4")
    lines, duration = transcribe("low.mp4", ccfg.get("whisper_model", "small"))
    if len(lines) < 20:
        mark("skipped-no-speech")
        return
    raw = gemini_chapters(lines, api_key, ccfg.get("model", "gemini-2.5-flash"))
    print("gemini raw:\n" + raw[:1000], file=sys.stderr)
    chapters = validate_chapters(raw, duration)
    if len(chapters) < 3:
        mark("skipped-too-few")
        return
    print("chapters:", file=sys.stderr)
    for s, label in chapters:
        print(f"  {hms(s)} {label}", file=sys.stderr)
    if a.dry_run:
        print("dry-run: not updating")
        return
    video_id = st["yt_url"].split("v=")[-1]
    update_youtube_description(video_id, chapters)
    mark("done", {"n": len(chapters)})
    print("description updated")


if __name__ == "__main__":
    main()
