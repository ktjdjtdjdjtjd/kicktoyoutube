"""配信終了ウォッチャー + バックログ消化スケジューラ (cron 15分おき想定)。

各チャンネルのVOD一覧から「終了済み・未処理」のVODを古い順に拾い、
ペース制御 (1日の投稿上限 / 同時処理数上限) の範囲で process.yml をdispatchする。

- config.backlog=true なら鮮度制限(max_vod_age_days)を外して過去アーカイブも対象にする
- 1日の枠は YouTube のクォータ日 (太平洋時間) 単位で数える
- state/<uuid>.json が台帳。dispatched のまま24時間経過したもの (=失敗run) は再投入する

    python watch.py [--config config.json] [--dry-run]

要件: 環境変数 GH_TOKEN (Actions内は github.token), gh CLI, git push権限。
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import kick_api
from chat_fetch import parse_dt
from repo_state import STATE_DIR, commit_state

PT = ZoneInfo("America/Los_Angeles")
STALE_HOURS = 12


def load_states():
    states = {}
    if STATE_DIR.exists():
        for p in STATE_DIR.glob("*.json"):
            try:
                states[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    return states


def latest_ts(st):
    best = None
    for key in ("dispatched_at", "marked_at", "updated_at"):
        v = st.get(key)
        if not v:
            continue
        try:
            dt = datetime.fromisoformat(v)
        except Exception:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if best is None or dt > best:
            best = dt
    return best


def pick_dispatches(videos, states, cfg, now):
    """[(slug付きvod情報)] を古い順に、ペース制御の範囲で返す。"""
    backlog = cfg.get("backlog", False)
    max_age = timedelta(days=cfg.get("max_vod_age_days", 3))
    min_end_age = timedelta(minutes=cfg.get("min_end_age_minutes", 20))
    daily_limit = cfg.get("daily_upload_limit", 6)
    max_inflight = cfg.get("max_inflight", 3)
    batch = cfg.get("dispatch_batch", 2)

    # ペースの現状
    inflight = 0
    today_count = 0
    today_pt = now.astimezone(PT).date()
    for st in states.values():
        status = str(st.get("status", ""))
        ts = latest_ts(st)
        if status.startswith("dispatched"):
            if ts and (now - ts) < timedelta(hours=STALE_HOURS):
                inflight += 1
        if status.startswith(("dispatched", "done")):
            if ts and ts.astimezone(PT).date() == today_pt:
                today_count += 1

    allowance = min(daily_limit - today_count, max_inflight - inflight, batch)
    print(f"pace: inflight={inflight} today(PT)={today_count} allowance={allowance}",
          file=sys.stderr)
    if allowance <= 0:
        return []

    candidates = []
    for v in videos:
        uuid = (v.get("video") or {}).get("uuid")
        if not uuid or v.get("is_live") or not v.get("duration"):
            continue
        try:
            start = parse_dt(v["start_time"])
        except Exception:
            continue
        if not backlog and now - start > max_age:
            continue
        ended_at = start + timedelta(milliseconds=v["duration"])
        if now - ended_at < min_end_age:
            continue
        st = states.get(uuid)
        if st:
            status = str(st.get("status", ""))
            ts = latest_ts(st)
            if status.startswith(("done", "skipped")):
                continue
            if status.startswith("dispatched") and ts and (now - ts) < timedelta(hours=STALE_HOURS):
                continue  # 処理中
            # dispatchedのまま24h超 = 失敗run → 再投入対象
        candidates.append({
            "slug": v["_slug"],
            "uuid": uuid,
            "title": v.get("session_title") or "",
            "start_time": v["start_time"],
            "duration_s": v["duration"] / 1000.0,
            "_start": start,
        })
    candidates.sort(key=lambda c: c["_start"])  # 古い順
    return candidates[:allowance]


def run(cmd, check=True):
    print("+ " + " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=check)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)
    STATE_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)

    videos = []
    for slug in cfg["channels"]:
        vs = kick_api.get_channel_videos(slug)
        print(f"{slug}: {len(vs)} vods", file=sys.stderr)
        for v in vs:
            v["_slug"] = slug
        videos.extend(vs)

    picks = pick_dispatches(videos, load_states(), cfg, now)
    if not picks:
        print("nothing to dispatch")
        return
    for c in picks:
        print(json.dumps({k: v for k, v in c.items() if k != "_start"},
                         ensure_ascii=False))
    if a.dry_run:
        return

    paths = []
    for c in picks:
        p = STATE_DIR / f"{c['uuid']}.json"
        prev = {}
        if p.exists():
            try:
                prev = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
        retries = int(prev.get("retries", 0)) + (1 if prev else 0)
        p.write_text(json.dumps({
            **{k: v for k, v in c.items() if k != "_start"},
            "status": "dispatched", "retries": retries,
            "dispatched_at": now.isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(p)

    # 先にstateをコミット (push失敗時はdispatchしない = 二重処理より未処理を選ぶ)
    if not commit_state(paths, f"state: dispatch {len(picks)} vod(s)"):
        sys.exit("error: could not push state — skip dispatch, retry next cycle")

    for c in picks:
        run(["gh", "workflow", "run", "process.yml",
             "-f", f"slug={c['slug']}", "-f", f"uuid={c['uuid']}"])
        print(f"dispatched: {c['slug']}/{c['uuid']} {c['title']}")


if __name__ == "__main__":
    main()
