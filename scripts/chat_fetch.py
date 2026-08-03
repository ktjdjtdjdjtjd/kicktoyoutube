"""Kickチャットの全量取得 (curl_cffi・5秒窓並列)。

    python chat_fetch.py <channel_id> <start_iso> <duration_s> --jsonl out.jsonl
"""
import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import kick_api

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


def fetch_all_chat(channel_id, start_dt, duration_s, workers=8, keep_emotes=False):
    """5秒窓で全区間を並列取得。[(rel_sec, content), ...] を時系列で返す。
    keep_emotes=True なら [emote:id:name] マーカーを原文のまま残す (画像焼き込み用)。"""
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
            if not keep_emotes:
                content = EMOTE_RE.sub(lambda mm: mm.group(1), content)
            content = content.replace("\n", "")
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel_id", type=int)
    ap.add_argument("start_iso")
    ap.add_argument("duration_s", type=float)
    ap.add_argument("--jsonl", default="chat.jsonl")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    start_dt = parse_dt(a.start_iso)
    msgs = fetch_all_chat(a.channel_id, start_dt, a.duration_s,
                          workers=a.workers, keep_emotes=True)
    with open(a.jsonl, "w", encoding="utf-8") as f:
        for rel, content in msgs:
            f.write(json.dumps({"rel": rel, "content": content}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
