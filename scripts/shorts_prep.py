"""ショート下書き素材の一括生成 (shorts_prep.yml から実行)。

依頼 queue_shorts/request.json:
  {"platform": "kick" | "twitch",
   "video": "<VOD URL>",
   "height": 720,
   "segments": [{"id": 1, "start": 1200.0, "end": 1290.0}, ...]}

各区間について: 部分DL → クリップ切り出し → faster-whisper字幕(SRT) →
Geminiでヘッダータイトル案(1行7〜9文字×2案)。out_shorts/ を artifact で返す。
"""
import argparse
import json
import re
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import kick_api
from burn_request import KICK_URL_RE, TWITCH_URL_RE
from chapters import GEMINI_URL, MODEL_FALLBACKS

TITLE_PROMPT = """以下はライブ配信の切り抜きクリップの文字起こしです。
縦型ショート動画の黒帯ヘッダーに載せる日本語タイトルを2案作ってください。

## 制約
- 1案は7〜9文字ちょうど(全角換算)。改行なし
- 内容に実際に出てくる出来事・発言だけを使う。創作・誇張禁止
- 煽り記号(!?)は各案1個まで。絵文字禁止

## 出力形式
- 2行だけ出力する。1行につき1案、タイトル本文だけを書く
- 番号やラベル("案1" など)、箇条書き記号、引用符、説明を付けない

## 文字起こし
{transcript}
"""

# "案1 ..." のようなラベルが返ることがあり、そのままヘッダーに焼かれてしまう
# (実測: 「案1 犯人はあのメガネの人!?」が焼き込まれた)。プロンプトだけに頼らず落とす。
TITLE_LABEL_RE = re.compile(r"^\s*(?:案\s*[0-9０-９]+|[0-9０-９]+|[-*・>＞])\s*[.):：、]?\s*")


def clean_title(s):
    s = (s or "").strip()
    s = TITLE_LABEL_RE.sub("", s)
    return s.strip().strip('"\'「」『』').strip()


def resolve_source(platform, video):
    if platform == "kick":
        m = KICK_URL_RE.search(video)
        if not m:
            sys.exit(f"error: kick URL不明: {video}")
        from plan import resolve_meta
        meta = resolve_meta(m.group(1), m.group(2))
        return meta.get("source") or video, meta.get("title", "")
    m = TWITCH_URL_RE.search(video)
    vid = m.group(1) if m else video
    return f"https://www.twitch.tv/videos/{vid}", ""


def cut_clip(source, start, end, dest, height):
    """区間だけ部分DL。sourceがm3u8ならyt-dlpのsection指定で必要分のみ取る。"""
    subprocess.run([
        "yt-dlp", "--impersonate", "chrome",
        "-f", f"bv*[height<=?{height}]+ba/b[height<=?{height}]/b",
        "--no-part", "--retries", "10", "--fragment-retries", "30",
        "--download-sections", f"*{start:.0f}-{end:.0f}",
        "--merge-output-format", "mp4",
        "-o", dest, source], check=True)


def transcribe_srt(model, clip, srt_path):
    wav = "clip_audio.wav"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", clip, "-vn", "-ac", "1", "-ar", "16000", wav],
                   check=True)
    segments, _ = model.transcribe(wav, language="ja", beam_size=1,
                                   vad_filter=True,
                                   vad_parameters={"min_silence_duration_ms": 700})
    def ts(t):
        h, m = int(t // 3600), int(t % 3600 // 60)
        s = t - h * 3600 - m * 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")
    lines = []
    texts = []
    n = 0
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        n += 1
        lines.append(f"{n}\n{ts(seg.start)} --> {ts(seg.end)}\n{text}\n")
        texts.append(text)
    Path(srt_path).write_text("\n".join(lines), encoding="utf-8")
    Path(wav).unlink(missing_ok=True)
    return texts


def gemini_titles(transcript, api_key):
    if not api_key or not transcript.strip():
        return []
    body = json.dumps({
        "contents": [{"parts": [{"text": TITLE_PROMPT.format(transcript=transcript[:3000])}]}],
        "generationConfig": {"temperature": 0.4},
    }).encode()
    for m in MODEL_FALLBACKS:
        req = urllib.request.Request(GEMINI_URL.format(model=m, key=api_key),
                                     data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read())
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            return [t for t in (clean_title(ln) for ln in text.splitlines()) if t][:2]
        except Exception as e:
            print(f"gemini {m}: {e}", file=sys.stderr)
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", default="queue_shorts/request.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out", default="out_shorts")
    a = ap.parse_args()
    kick_api.load_config(a.config)
    req = json.loads(Path(a.request).read_text(encoding="utf-8"))
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    height = int(req.get("height") or 720)
    api_key = os.environ.get("GEMINI_API_KEY", "")

    source, vod_title = resolve_source(req["platform"], req["video"])
    print(f"source resolved: {vod_title}", file=sys.stderr)

    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")

    manifest = {"video": req["video"], "title": vod_title, "clips": []}
    failed = 0
    for seg in req["segments"]:
        cid = int(seg["id"])
        clip = outdir / f"clip_{cid:02d}.mp4"
        srt = outdir / f"clip_{cid:02d}.srt"
        try:
            cut_clip(source, float(seg["start"]), float(seg["end"]), str(clip), height)
            texts = transcribe_srt(model, str(clip), str(srt))
            titles = gemini_titles(" ".join(texts), api_key)
            manifest["clips"].append({
                "id": cid, "start": seg["start"], "end": seg["end"],
                "file": clip.name, "srt": srt.name,
                "n_lines": len(texts), "titles": titles,
                # 候補選定が付けた材料。朝に見比べて選ぶときの判断根拠になるので残す
                **{k: seg[k] for k in ("score", "rel", "msgs", "tags", "comments")
                   if k in seg},
            })
            print(f"clip {cid}: {len(texts)} lines, titles={titles}", file=sys.stderr)
        except Exception as e:
            failed += 1
            print(f"clip {cid} FAILED: {e}", file=sys.stderr)
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {len(manifest['clips'])} clips, {failed} failed")
    if not manifest["clips"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
