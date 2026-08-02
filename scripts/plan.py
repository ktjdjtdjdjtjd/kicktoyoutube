"""process の第1段: VODメタ解決 + チャット全量DL + full.ass生成 + セグメント計画。

    python plan.py <slug> <uuid> --out out/ [--config config.json] [--limit-windows N]

出力:
  out/meta.json  … title / duration_s / start_time / channel_id / url / segments
  out/full.ass   … 全区間ダンマク
  out/chat.jsonl … 生ログ (rel秒, content)
GITHUB_OUTPUT があれば matrix / title / duration_s / date を書き込む。
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

import kick_api
import chat_to_ass


def plan_segments(duration_s, segment_seconds):
    """[{'idx':0,'start':0,'end':5400}, ...]。端数が20分未満なら前セグメントに併合。"""
    n = max(1, math.ceil(duration_s / segment_seconds))
    segs = []
    for i in range(n):
        start = i * segment_seconds
        end = min((i + 1) * segment_seconds, duration_s)
        segs.append({"idx": i, "start": start, "end": end})
    if len(segs) >= 2 and (segs[-1]["end"] - segs[-1]["start"]) < 1200:
        segs[-2]["end"] = segs[-1]["end"]
        segs.pop()
    return segs


def resolve_meta(slug, uuid):
    """URLのuuidが旧v4(=一覧のvideo.uuid)でも新v7(=web.kick.comのid)でも解決する。
    v2一覧エントリの source (m3u8) を必ず拾う (yt-dlpのkick抽出器はv7 uuidで404するため)。"""
    videos = kick_api.get_channel_videos(slug)
    entry = next((v for v in videos if (v.get("video") or {}).get("uuid") == uuid), None)
    web = None
    if entry is None:
        ch = kick_api.get_channel(slug)
        if not ch:
            raise RuntimeError(f"channel not found: {slug}")
        web = kick_api.get_video_meta_web(ch["id"], uuid)
        if not web:
            raise RuntimeError(f"video not found: {slug}/{uuid}")
        ws = chat_to_ass.parse_dt(web["start_time"])
        for v in videos:
            try:
                vs = chat_to_ass.parse_dt(v["start_time"])
            except Exception:
                continue
            if abs((vs - ws).total_seconds()) < 120:
                entry = v
                break
    if entry is None and web is None:
        raise RuntimeError(f"video not found: {slug}/{uuid}")
    meta = {}
    if web:
        meta = {
            "title": web.get("title") or uuid,
            "duration_s": float(web.get("duration") or 0),
            "start_time": web.get("start_time"),
            "channel_id": (web.get("channel") or {}).get("id"),
            "is_live": bool(web.get("is_live")),
            "source": None,
        }
    if entry:
        meta.update({
            "title": entry.get("session_title") or meta.get("title") or uuid,
            "duration_s": (entry.get("duration") or 0) / 1000.0,
            "start_time": entry.get("start_time"),
            "channel_id": entry.get("channel_id") or meta.get("channel_id"),
            "is_live": bool(entry.get("is_live")),
            "source": entry.get("source"),
        })
    return meta


def gh_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        if "\n" in str(value):
            f.write(f"{key}<<__EOF__\n{value}\n__EOF__\n")
        else:
            f.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("uuid")
    ap.add_argument("--out", default="out")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--limit-windows", type=int, default=0,
                    help="スモークテスト用: チャット取得をN窓(5秒/窓)に制限")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)

    meta = resolve_meta(a.slug, a.uuid)
    if meta["is_live"]:
        raise RuntimeError("VOD is still live — abort")
    if meta["duration_s"] <= 0:
        raise RuntimeError("duration is 0 — VOD not finalized yet")
    print(f"title={meta['title']} duration={meta['duration_s']}s "
          f"channel_id={meta['channel_id']} start={meta['start_time']}", file=sys.stderr)

    start_dt = chat_to_ass.parse_dt(meta["start_time"])
    chat_dur = meta["duration_s"]
    if a.limit_windows:
        chat_dur = min(chat_dur, a.limit_windows * 5)
    msgs = chat_to_ass.fetch_all_chat(
        meta["channel_id"], start_dt, chat_dur, workers=cfg.get("chat_workers", 8))
    with open(outdir / "chat.jsonl", "w", encoding="utf-8") as f:
        for rel, content in msgs:
            f.write(json.dumps({"rel": rel, "content": content}, ensure_ascii=False) + "\n")
    chat_to_ass.write_ass(msgs, str(outdir / "full.ass"))

    segments = plan_segments(meta["duration_s"], cfg.get("segment_seconds", 5400))
    date = str(meta["start_time"])[:10]
    meta_out = {
        "slug": a.slug,
        "uuid": a.uuid,
        "url": f"https://kick.com/{a.slug}/videos/{a.uuid}",
        "source": meta.get("source"),
        "title": meta["title"],
        "duration_s": meta["duration_s"],
        "start_time": meta["start_time"],
        "channel_id": meta["channel_id"],
        "date": date,
        "segments": segments,
        "n_messages": len(msgs),
    }
    (outdir / "meta.json").write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8")

    gh_output("matrix", json.dumps(segments))
    gh_output("title", meta["title"])
    gh_output("date", date)
    gh_output("duration_s", str(meta["duration_s"]))
    print(json.dumps(meta_out, ensure_ascii=False))


if __name__ == "__main__":
    main()
