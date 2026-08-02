"""Kickチャットの全量取得 + 白ダンマクASS生成 (BIZ UDPGothic)。

kick_chat.py (stream-chat-burn) を移植し、通信を curl_cffi (CF対策) に置換したもの。
ライブラリとして plan.py から呼ぶほか、CLI 単体でも動く:

    python chat_to_ass.py <channel_id> <start_iso> <duration_s> <out.ass> [--jsonl out.jsonl]
"""
import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import kick_api

LANES = [45, 116, 187, 258, 329, 400, 471, 542, 613, 684, 755, 826, 897, 968]
FONT_PX_FULL = 65
FONT_PX_HALF = 32
DURATION_DISPLAY = 10.0
SCREEN_W = 1920
EMOTE_RE = re.compile(r"\[emote:\d+:([^\]]+)\]")


def parse_dt(s):
    dt = None
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            break
        except Exception:
            continue
    if dt is None:
        raise ValueError(f"unparseable datetime: {s}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_all_chat(channel_id, start_dt, duration_s, workers=8):
    """5秒窓で全区間を並列取得。[(rel_sec, content), ...] を時系列で返す。"""
    offsets = list(range(0, int(duration_s) + 5, 5))
    print(f"chat windows: {len(offsets)}", file=sys.stderr)
    all_msgs = []
    seen_ids = set()
    lock = threading.Lock()
    done = [0]

    def worker(off):
        t = start_dt + timedelta(seconds=off)
        iso = t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        msgs = kick_api.get_chat_window(channel_id, iso)
        local = []
        for m in msgs:
            mid = m.get("id")
            created = m.get("created_at")
            sender = m.get("sender") or {}
            content = m.get("content") or ""
            if not (mid and created and sender.get("username") and content):
                continue
            try:
                rel = (parse_dt(created) - start_dt).total_seconds()
            except Exception:
                continue
            if rel < -5 or rel > duration_s + 30:
                continue
            content = EMOTE_RE.sub(lambda mm: mm.group(1), content).replace("\n", "")
            if m.get("type") == "reply":
                try:
                    md = json.loads(m.get("metadata") or "{}")
                    orig = (md.get("original_message") or {}).get("sender", {}).get("username")
                    if orig:
                        content = f"@{orig} {content}"
                except Exception:
                    pass
            local.append((mid, rel, content))
        with lock:
            for mid, rel, content in local:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                all_msgs.append((rel, content))
            done[0] += 1
            if done[0] % 100 == 0 or done[0] == len(offsets):
                print(f"progress: {done[0]}/{len(offsets)} msgs={len(all_msgs)}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(worker, offsets))
    all_msgs.sort(key=lambda x: x[0])
    return all_msgs


def estimate_width(text):
    w = 0
    for ch in text:
        w += FONT_PX_HALF if ord(ch) < 128 else FONT_PX_FULL
    return w


def ass_time(seconds):
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


ASS_HEADER = """﻿[Script Info]
Title: Chat Danmaku
ScriptType: v4.00+
WrapStyle: 2
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,BIZ UDPGothic,65,&HFFFFFF&,&HFFFFFF&,&H000000&,&H000000&,1,0,0,0,100,100,0,0,1,2,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""


def write_ass(msgs, out_path):
    lane_free = [0.0] * len(LANES)
    out = [ASS_HEADER]
    for rel, msg in msgs:
        text = msg.replace("\n", " ").replace("{", "(").replace("}", ")")
        width = estimate_width(text)
        end_x = -width - 50
        lane_idx = None
        for i, free_at in enumerate(lane_free):
            if free_at <= rel:
                lane_idx = i
                break
        if lane_idx is None:
            lane_idx = int(rel * 1000) % len(LANES)
        speed = (SCREEN_W - end_x) / DURATION_DISPLAY
        busy_until = rel + (width / speed) + 0.3
        lane_free[lane_idx] = busy_until
        y = LANES[lane_idx]
        out.append(
            f"Dialogue: 0,{ass_time(rel)},{ass_time(rel + DURATION_DISPLAY)},Danmaku,,0,0,0,,"
            f"{{\\an4\\move({SCREEN_W},{y},{end_x},{y})\\c&HFFFFFF&\\3c&H000000&\\bord2\\alpha&H66&}}{text}"
        )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"Saved: {out_path} ({len(msgs)} events)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel_id", type=int)
    ap.add_argument("start_iso")
    ap.add_argument("duration_s", type=float)
    ap.add_argument("out_ass")
    ap.add_argument("--jsonl", default="")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    start_dt = parse_dt(a.start_iso)
    msgs = fetch_all_chat(a.channel_id, start_dt, a.duration_s, workers=a.workers)
    if a.jsonl:
        with open(a.jsonl, "w", encoding="utf-8") as f:
            for rel, content in msgs:
                f.write(json.dumps({"rel": rel, "content": content}, ensure_ascii=False) + "\n")
    write_ass(msgs, a.out_ass)


if __name__ == "__main__":
    main()
