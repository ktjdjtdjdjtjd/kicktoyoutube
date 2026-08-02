"""配信終了ウォッチャー (cron 15分おき想定)。

各チャンネルのVOD一覧を見て「終了済み・未処理・鮮度内」のVODを検知したら、
state/<uuid>.json を先にコミット (二重処理ガード) してから
process.yml を workflow_dispatch で起動する。

    python watch.py [--config config.json] [--dry-run]

要件: 環境変数 GH_TOKEN (Actions内は github.token), gh CLI, git push権限。
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import kick_api
from chat_to_ass import parse_dt

STATE_DIR = Path("state")


def find_new_vods(cfg):
    now = datetime.now(timezone.utc)
    max_age = timedelta(days=cfg.get("max_vod_age_days", 3))
    min_end_age = timedelta(minutes=cfg.get("min_end_age_minutes", 20))
    found = []
    for slug in cfg["channels"]:
        videos = kick_api.get_channel_videos(slug)
        print(f"{slug}: {len(videos)} vods", file=sys.stderr)
        for v in videos:
            uuid = (v.get("video") or {}).get("uuid")
            if not uuid or v.get("is_live") or not v.get("duration"):
                continue
            state_file = STATE_DIR / f"{uuid}.json"
            if state_file.exists():
                continue
            try:
                start = parse_dt(v["start_time"])
            except Exception:
                continue
            if now - start > max_age:
                continue
            ended_at = start + timedelta(milliseconds=v["duration"])
            if now - ended_at < min_end_age:
                print(f"  {uuid}: ended too recently, wait next cycle", file=sys.stderr)
                continue
            found.append({
                "slug": slug,
                "uuid": uuid,
                "title": v.get("session_title") or "",
                "start_time": v["start_time"],
                "duration_s": v["duration"] / 1000.0,
            })
    return found


def run(cmd, check=True):
    print("+ " + " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=check)


def commit_state(paths, message):
    run(["git", "config", "user.name", "kick-archive-bot"])
    run(["git", "config", "user.email", "actions@users.noreply.github.com"])
    run(["git", "add"] + [str(p) for p in paths])
    r = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if r.returncode == 0:
        print("nothing to commit", file=sys.stderr)
        return True
    run(["git", "commit", "-m", message])
    for attempt in range(3):
        if subprocess.run(["git", "push"]).returncode == 0:
            return True
        print(f"push failed, rebase retry {attempt+1}", file=sys.stderr)
        run(["git", "pull", "--rebase"], check=False)
        time.sleep(3)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)
    STATE_DIR.mkdir(exist_ok=True)

    vods = find_new_vods(cfg)
    if not vods:
        print("no new vods")
        return
    print(json.dumps(vods, ensure_ascii=False, indent=2))
    if a.dry_run:
        return

    paths = []
    for v in vods:
        p = STATE_DIR / f"{v['uuid']}.json"
        p.write_text(json.dumps({**v, "status": "dispatched",
                                 "dispatched_at": datetime.now(timezone.utc).isoformat()},
                                ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(p)

    # 先にstateをコミット (push失敗時はdispatchしない = 二重処理より未処理を選ぶ)
    if not commit_state(paths, f"state: dispatch {len(vods)} vod(s)"):
        sys.exit("error: could not push state — skip dispatch, retry next cycle")

    for v in vods:
        run(["gh", "workflow", "run", "process.yml",
             "-f", f"slug={v['slug']}", "-f", f"uuid={v['uuid']}"])
        print(f"dispatched: {v['slug']}/{v['uuid']} {v['title']}")


if __name__ == "__main__":
    main()
