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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import kick_api
from repo_state import STATE_DIR, commit_paths

# タイムスタンプトークン (前後が数字/コロンでない位置のみ = 0:11:15 の 11:15 に誤マッチしない)
TS_TOKEN = r"(?<![\d:])(?:\d{1,2}:)?\d{1,2}:\d{2}(?![\d:])"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent?key={key}")

PROMPT = """以下はライブ配信の文字起こしです。各行の先頭に実測のタイムスタンプが付いています。
この配信のYouTube用チャプター(タイムスタンプ)を作成してください。

## 出力形式(これ以外は一切出力しない。1行に1章、必ず改行区切り)
00:00 配信開始
HH:MM:SS 見出し
HH:MM:SS 見出し

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
    """表示形式: 60分未満=MM:SS、1時間以降=HH:MM:SS (どちらもゼロ埋め)。"""
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


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
        if int(d.get("chapters_attempts", 0)) >= 5:
            continue  # 失敗が続く動画は諦める (無限リトライ防止)
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


def download_window(source_url, start, end, dest):
    """sourceの [start,end) 区間だけを最低画質でDL (文字起こしは音声のみで足りる)。
    HLSでも --download-sections が該当フラグメントだけ取得するため帯域を抑えられる。"""
    subprocess.run(["yt-dlp", "--impersonate", "chrome",
                    "-f", "wa*/w/b", "--no-part",
                    "--download-sections", f"*{int(start)}-{int(end)}",
                    "--retries", "30", "--fragment-retries", "60",
                    "--concurrent-fragments", "4",
                    "--merge-output-format", "mp4",
                    "-o", dest, source_url], check=True)


def extract_audio(src, dest="audio.wav"):
    """動画→16kHzモノラルwav。長尺VODの一括デコードによるメモリ圧を避ける下拵え。"""
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", src, "-vn", "-ac", "1", "-ar", "16000", dest],
                   check=True)
    Path(src).unlink(missing_ok=True)
    return dest


def transcribe(path, model_size="small", chunk_s=1800):
    """30分チャンクずつ文字起こし (4時間級VODの一括処理はランナーVMごと
    落ちる=exit143 が実測されたため、常にメモリ有界にする)。"""
    from faster_whisper import WhisperModel
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    total = float(r.stdout.strip())
    lines = []
    t = 0.0
    part = "chunk.wav"
    while t < total:
        clen = min(chunk_s, total - t)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", f"{t:.3f}", "-t", f"{clen:.3f}", "-i", path, part],
                       check=True)
        segments, _info = model.transcribe(part, language="ja", beam_size=1,
                                           vad_filter=True,
                                           vad_parameters={"min_silence_duration_ms": 1500})
        for seg in segments:
            text = seg.text.strip()
            if text:
                lines.append((t + seg.start, text))
        print(f"transcribe chunk {t:.0f}-{t + clen:.0f}s: lines={len(lines)}",
              file=sys.stderr)
        t += clen
    Path(part).unlink(missing_ok=True)
    print(f"transcribed: {len(lines)} lines, duration={total:.0f}s",
          file=sys.stderr)
    return lines, total


# バージョン固定名(gemini-2.5-flash等)は本番で404になった(モデル廃止)。
# Googleが維持する -latest エイリアス系のみ使う(gemini-flash-latestは本番で疎通確認済み)。
# 404のモデルはリトライロジックが自動でスキップするので、多めに並べても害はない。
MODEL_FALLBACKS = ["gemini-flash-latest", "gemini-flash-lite-latest",
                   "gemini-pro-latest", "gemini-2.5-flash"]


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
    import time
    models = [model] + [m for m in MODEL_FALLBACKS if m != model]
    # 全モデルを一巡してもレート制限/5xxが続くなら、モデル巡回ごと数回リトライする
    # (429=レート制限, 500/502/503/504=Geminiの一時障害。どちらも待てば回復する)
    last_err = None
    for cycle in range(3):
        transient_all = True
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
                    print(f"gemini {m}: HTTP {e.code} (cycle {cycle} attempt {attempt})",
                          file=sys.stderr)
                    last_err = e
                    if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                        time.sleep(75 if e.code == 429 else 20)
                        continue
                    if e.code not in (429, 500, 502, 503, 504):
                        transient_all = False  # 404/400等は待っても直らない
                    break  # 次のモデル候補へ
                except urllib.error.URLError as e:
                    print(f"gemini {m}: URLError {e} (cycle {cycle})", file=sys.stderr)
                    last_err = e
                    break
        if not transient_all:
            break  # 一時障害でない失敗が混じっていれば巡回しても無駄
        time.sleep(30)  # 全モデル一時障害 → 少し待って巡回リトライ
    raise last_err


def extract_pairs(text):
    """テキストから (秒, 見出し) を抽出。Geminiが改行せず1行に複数章を
    連結して返しても、タイムスタンプトークンを区切りに分解できる。"""
    parts = re.split(f"({TS_TOKEN})", text)
    pairs = []
    for i in range(1, len(parts) - 1, 2):
        sec = parse_ts(parts[i])
        label = parts[i + 1].splitlines()[0] if parts[i + 1].strip() else ""
        label = label.strip(" \t-•:：、。")
        if sec is not None and label:
            pairs.append((sec, label))
    return pairs


def validate_chapters(raw, duration_s):
    """出力を検証・正規化。[(sec, label)] を返す。"""
    out = []
    for sec, label in extract_pairs(raw):
        if sec > duration_s or len(label) > 40:
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


def update_youtube_description(video_id, chapters, token_env="YT_TOKEN_JSON"):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    info = json.loads(os.environ[token_env])
    creds = Credentials.from_authorized_user_info(info)  # 付与済みスコープをそのまま使う
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


def load_state_by_uuid(uuid):
    """uuidから状態ファイル(path, dict)を引く。finalizeが素の再取得に使う。"""
    for p in STATE_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("uuid") == uuid:
            return p, d
    return None, None


TRANSCRIPT_DIR = Path("transcripts")


def format_transcript(st, lines):
    """保存用の人が読める文字起こしテキストを組み立てる。
    アーカイブ内は全行 HH:MM:SS 固定 (表記統一でgrep・再パースしやすい)。"""
    def hms_full(sec):
        s = int(sec)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    head = (f"# {st.get('title', '')}\n{st.get('yt_url', '')}\n"
            f"slug: {st.get('slug', '')}  lines: {len(lines)}\n\n")
    body = "\n".join(f"[{hms_full(t)}] {txt}" for t, txt in lines)
    return head + body + "\n"


def save_transcript(st, lines):
    """結合済み文字起こしを transcripts/<uuid>.md に永続保存 (人が読める形+再利用可)。
    Geminiより前に呼ぶことで、章立てが失敗しても文字起こしの成果を失わない。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    p = TRANSCRIPT_DIR / f"{st.get('uuid', 'unknown')}.md"
    p.write_text(format_transcript(st, lines), encoding="utf-8")
    commit_paths([p], f"transcript: {st.get('uuid', '')[:8]} ({len(lines)} lines)",
                 fatal=False)
    return p


def _mark(path, st, result, extra=None):
    st["chapters"] = {"result": result,
                      "at": datetime.now(timezone.utc).isoformat(), **(extra or {})}
    path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    commit_paths([path], f"state: chapters {result} ({st.get('uuid', '')[:8]})",
                 fatal=False)


def slice_part_lines(lines, part_start, part_dur):
    """分割投稿のパートに属する文字起こし行を、パート内時刻(0起点)へ変換して返す。"""
    end = part_start + part_dur
    return [(t - part_start, txt) for t, txt in lines if part_start <= t < end]


def _gemini_with_rollback(path, st, lines, ccfg, api_key, dry_run):
    """Gemini章立て。一時障害(429/5xx/接続不可)は試行消費を戻して None を返す
    (呼び元はそのまま return し、次サイクルで再挑戦する。赤failにしない)。"""
    try:
        return gemini_chapters(lines, api_key, ccfg.get("model", "gemini-flash-latest"))
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        code = getattr(e, "code", None)
        if code is None or code in (429, 500, 502, 503, 504):
            if not dry_run:
                st["chapters_attempts"] = max(0, int(st.get("chapters_attempts", 1)) - 1)
                path.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                commit_paths([path], f"state: chapters gemini一時障害でリトライ待ち "
                                     f"({st.get('uuid', '')[:8]})", fatal=False)
            print(f"gemini transient failure ({code}) — 次サイクルで再試行", file=sys.stderr)
            return None
        raise


def _finish_with_gemini(path, st, lines, duration, cfg, ccfg, api_key, dry_run):
    """文字起こし→Gemini章立て→検証→YouTube説明欄更新→状態確定。
    12h超で分割投稿された動画(st['parts'])は、パートごとに文字起こしを切り出し
    パート内時刻(0起点)で章立てして各動画の説明欄を更新する。"""
    if len(lines) < 20:
        _mark(path, st, "skipped-no-speech")
        return
    save_transcript(st, lines)  # 章立ての前に保存 (Gemini失敗でも成果を残す)
    token_env = ((cfg.get("channel_settings") or {}).get(st["slug"], {})
                 .get("yt_token_env", "YT_TOKEN_JSON"))
    parts = st.get("parts") or []
    if len(parts) > 1:
        counts = []
        for i, p in enumerate(parts, 1):
            p_start = float(p.get("start_s") or 0.0)
            p_dur = float(p.get("duration_s") or max(0.0, duration - p_start))
            plines = slice_part_lines(lines, p_start, p_dur)
            if len(plines) < 20:
                print(f"part {i}/{len(parts)}: 発話が少ない — skip", file=sys.stderr)
                continue
            raw = _gemini_with_rollback(path, st, plines, ccfg, api_key, dry_run)
            if raw is None:
                return  # 一時障害: 全体を次サイクルへ (更新はin-place置換なので冪等)
            chs = validate_chapters(raw, p_dur)
            if len(chs) < 3:
                print(f"part {i}/{len(parts)}: 章が少なすぎる — skip", file=sys.stderr)
                continue
            print(f"chapters (part {i}/{len(parts)}):", file=sys.stderr)
            for s, label in chs:
                print(f"  {hms(s)} {label}", file=sys.stderr)
            if not dry_run:
                update_youtube_description(p["url"].split("v=")[-1], chs, token_env)
            counts.append(len(chs))
        if dry_run:
            print("dry-run: not updating")
            return
        if not counts:
            _mark(path, st, "skipped-too-few")
            return
        _mark(path, st, "done", {"n": sum(counts), "parts_done": len(counts)})
        print("description updated (parts)")
        return
    raw = _gemini_with_rollback(path, st, lines, ccfg, api_key, dry_run)
    if raw is None:
        return
    print("gemini raw:\n" + raw[:1000], file=sys.stderr)
    chapters = validate_chapters(raw, duration)
    if len(chapters) < 3:
        _mark(path, st, "skipped-too-few")
        return
    print("chapters:", file=sys.stderr)
    for s, label in chapters:
        print(f"  {hms(s)} {label}", file=sys.stderr)
    if dry_run:
        print("dry-run: not updating")
        return
    video_id = st["yt_url"].split("v=")[-1]
    update_youtube_description(video_id, chapters, token_env)
    _mark(path, st, "done", {"n": len(chapters)})
    print("description updated")


def cmd_plan(cfg, ccfg, only_uuid, dry_run, out_dir="out"):
    """候補を1本選び、試行を消費し、文字起こしを区間分割したmatrixを出力する。
    resolve_metaは duration_s と source を返すのでffprobe不要。"""
    from plan import gh_output, plan_segments, resolve_meta
    path, st = pick_candidate(only_uuid or None)
    if not st:
        print("no candidate")
        gh_output("skip", "true")
        return
    print(f"target: {st.get('title')} {st['yt_url']}", file=sys.stderr)
    if not dry_run:
        st["chapters_attempts"] = int(st.get("chapters_attempts", 0)) + 1
        path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        commit_paths([path], f"state: chapters attempt {st['chapters_attempts']} "
                             f"({st.get('uuid', '')[:8]})", fatal=False)
    try:
        meta = resolve_meta(st["slug"], st["uuid"])
        if not meta.get("source") or meta.get("duration_s", 0) <= 0:
            raise RuntimeError("source/duration unavailable (VOD expired?)")
    except Exception as e:
        print(f"meta failed: {e}", file=sys.stderr)
        _mark(path, st, "skipped-no-source")
        gh_output("skip", "true")
        return
    seg_s = int(ccfg.get("transcribe_segment_seconds", 2700))
    segs = plan_segments(meta["duration_s"], seg_s)
    token_env = ((cfg.get("channel_settings") or {}).get(st["slug"], {})
                 .get("yt_token_env", "YT_TOKEN_JSON"))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / "chapters_plan.json").write_text(json.dumps({
        "uuid": st["uuid"], "slug": st["slug"], "yt_url": st["yt_url"],
        "duration_s": meta["duration_s"], "token_env": token_env,
        "n_segments": len(segs),
    }, ensure_ascii=False), encoding="utf-8")
    gh_output("skip", "false")
    gh_output("uuid", st["uuid"])
    gh_output("slug", st["slug"])
    gh_output("matrix", json.dumps(segs))
    print(f"plan: {len(segs)} segments x {seg_s}s (duration {meta['duration_s']:.0f}s)",
          file=sys.stderr)


def cmd_transcribe(ccfg, slug, uuid, idx, start, end, out_dir="segs"):
    """区間 [start,end) だけDL→文字起こしし、絶対時刻の行を seg_NNN.json に出力。
    source は毎回 resolve_meta で取り直す (署名URL失効に強くする)。"""
    from plan import resolve_meta
    meta = resolve_meta(slug, uuid)
    if not meta.get("source"):
        raise RuntimeError("source unavailable")
    win = f"win_{idx:03d}.mp4"
    download_window(meta["source"], start, end, win)
    audio = extract_audio(win, f"win_{idx:03d}.wav")
    lines, _ = transcribe(audio, ccfg.get("whisper_model", "small"))
    Path(audio).unlink(missing_ok=True)
    abs_lines = [[float(start) + t, txt] for t, txt in lines]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / f"seg_{idx:03d}.json").write_text(
        json.dumps({"idx": idx, "start": start, "end": end, "lines": abs_lines},
                   ensure_ascii=False), encoding="utf-8")
    print(f"transcribe seg {idx}: {len(abs_lines)} lines [{start:.0f}-{end:.0f}s]",
          file=sys.stderr)


def cmd_finalize(cfg, ccfg, api_key, dry_run, plan_dir="out", seg_dir="segs"):
    """各区間の seg_*.json を結合→Gemini章立て→説明欄更新。欠損区間があっても
    残りで続行する (単一区間のDL失敗で全体を赤failにして試行を溶かさない)。"""
    plan_file = Path(plan_dir) / "chapters_plan.json"
    if not plan_file.exists():
        print("no chapters_plan.json — nothing to finalize")
        return
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    path, st = load_state_by_uuid(plan["uuid"])
    if not st:
        print(f"state not found for {plan['uuid']}", file=sys.stderr)
        return
    seg_files = sorted(Path(seg_dir).rglob("seg_*.json"))
    lines = []
    for f in seg_files:
        try:
            lines.extend(json.loads(f.read_text(encoding="utf-8")).get("lines", []))
        except Exception as e:
            print(f"{f}: {e} — skip", file=sys.stderr)
    lines.sort(key=lambda x: x[0])
    got, want = len(seg_files), int(plan.get("n_segments", 0))
    print(f"finalize: {len(lines)} lines from {got}/{want} segments", file=sys.stderr)
    if want and got < want:
        print(f"WARNING: {want - got} 区間が欠損 — 残りで続行", file=sys.stderr)
    _finish_with_gemini(path, st, lines, float(plan["duration_s"]),
                        cfg, ccfg, api_key, dry_run)


def cmd_all(cfg, ccfg, api_key, only_uuid, dry_run):
    """単一ジョブで一気通貫 (ローカル/dispatch用の後方互換パス)。"""
    from plan import resolve_meta
    path, st = pick_candidate(only_uuid or None)
    if not st:
        print("no candidate")
        return
    print(f"target: {st.get('title')} {st['yt_url']}", file=sys.stderr)
    if not dry_run:
        st["chapters_attempts"] = int(st.get("chapters_attempts", 0)) + 1
        path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        commit_paths([path], f"state: chapters attempt {st['chapters_attempts']} "
                             f"({st.get('uuid', '')[:8]})", fatal=False)
    try:
        meta = resolve_meta(st["slug"], st["uuid"])
        if not meta.get("source"):
            raise RuntimeError("source unavailable (VOD expired?)")
    except Exception as e:
        print(f"meta failed: {e}", file=sys.stderr)
        _mark(path, st, "skipped-no-source")
        return
    download_audio(meta["source"], "low.mp4")
    audio = extract_audio("low.mp4")
    lines, duration = transcribe(audio, ccfg.get("whisper_model", "small"))
    _finish_with_gemini(path, st, lines, duration, cfg, ccfg, api_key, dry_run)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--uuid", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mode", default="all",
                    choices=["all", "plan", "transcribe", "finalize"])
    ap.add_argument("--slug", default="")
    ap.add_argument("--idx", type=int, default=0)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0)
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)
    ccfg = cfg.get("chapters", {})
    if not ccfg.get("enabled", True):
        print("chapters disabled")
        return

    if a.mode == "transcribe":
        cmd_transcribe(ccfg, a.slug, a.uuid, a.idx, a.start, a.end)
        return
    if a.mode == "plan":
        cmd_plan(cfg, ccfg, a.uuid, a.dry_run)
        return

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("GEMINI_API_KEY not set — skip")
        return
    if a.mode == "finalize":
        cmd_finalize(cfg, ccfg, api_key, a.dry_run)
    else:
        cmd_all(cfg, ccfg, api_key, a.uuid or None, a.dry_run)


if __name__ == "__main__":
    main()
