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
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import kick_api
from chat_fetch import parse_dt
from repo_state import STATE_DIR, commit_state

PT = ZoneInfo("America/Los_Angeles")
STALE_HOURS = 12       # 通常の失敗リトライ待ち
QUICK_STALE_HOURS = 2  # 処理が1本も動いていない場合の短縮リトライ待ち (障害復帰の高速化)
MAX_ATTEMPTS = 5       # 同一VODの自動投入は累計5回まで。超えたら needs-review で自動停止
                       # (再開は人がstateの retries を0に戻し status を消す。自動では再開しない)


def repo_has_active_process_runs():
    """processのqueued/in_progressが存在するか。判定不能ならNone(保守的に12h側)。"""
    tok = os.environ.get("GH_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not tok or not repo:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/actions/runs?per_page=30",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "kick-archive-watch"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        for r in d.get("workflow_runs", []):
            if r.get("name") == "process" and r.get("status") in ("queued", "in_progress"):
                return True
        return False
    except Exception as e:
        print(f"active-runs check failed: {e}", file=sys.stderr)
        return None


def dispatched_is_stale(ts, now, active_runs):
    """dispatched状態の再投入判定。activeが確実にFalseなら2h、それ以外は12h。"""
    if ts is None:
        return True
    age = now - ts
    if active_runs is False and age > timedelta(hours=QUICK_STALE_HOURS):
        return True
    return age > timedelta(hours=STALE_HOURS)


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


def pick_dispatches(videos, states, cfg, now, active_runs=None):
    """(投入候補リスト, 再試行上限超過リスト) を返す。候補はチャンネル優先度→古い順。"""
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
            if not dispatched_is_stale(ts, now, active_runs):
                inflight += 1
        if status.startswith(("dispatched", "done")):
            if ts and ts.astimezone(PT).date() == today_pt:
                today_count += 1

    allowance = min(daily_limit - today_count, max_inflight - inflight, batch)
    print(f"pace: inflight={inflight} today(PT)={today_count} allowance={allowance}",
          file=sys.stderr)

    candidates = []
    exhausted = []
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
            if status.startswith(("done", "skipped", "needs-review")):
                continue  # needs-review = 人の判断待ち。自動では二度と投入しない
            if status.startswith("dispatched") and not dispatched_is_stale(ts, now, active_runs):
                continue  # 処理中
            # staleなdispatched = 失敗run → 再投入対象。ただし累計上限で自動停止
            if status.startswith("dispatched") and int(st.get("retries", 0)) >= MAX_ATTEMPTS - 1:
                exhausted.append({"slug": v["_slug"], "uuid": uuid,
                                  "title": v.get("session_title") or ""})
                continue
        candidates.append({
            "slug": v["_slug"],
            "uuid": uuid,
            "title": v.get("session_title") or "",
            "start_time": v["start_time"],
            "duration_s": v["duration"] / 1000.0,
            "_start": start,
        })
    # チャンネル優先度 (config.channelsの並び順) → 同一チャンネル内は古い順
    prio = {slug: i for i, slug in enumerate(cfg.get("channels", []))}
    candidates.sort(key=lambda c: (prio.get(c["slug"], 99), c["_start"]))
    if allowance <= 0:
        return [], exhausted  # 枠が無くても上限超過の記録は行う
    return candidates[:allowance], exhausted


def run(cmd, check=True):
    print("+ " + " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, check=check)


def shorts_targets(picks, cfg):
    """ショート下書きを回すVOD。config.shorts_channels に入れたチャンネルだけ。

    アーカイブ(process)は全チャンネルを回すが、ショート候補は自分が編集する
    チャンネルにしか要らない。未設定なら何もしない。
    """
    chans = set(cfg.get("shorts_channels") or [])
    return [c for c in picks if c.get("slug") in chans]


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

    active = repo_has_active_process_runs()
    print(f"active process runs: {active}", file=sys.stderr)
    picks, exhausted = pick_dispatches(videos, load_states(), cfg, now,
                                       active_runs=active)

    # 累計上限に達した案件を needs-review 化して自動投入を停止 (人の判断待ち)
    if exhausted and not a.dry_run:
        paths = []
        for e in exhausted:
            p = STATE_DIR / f"{e['uuid']}.json"
            prev = {}
            if p.exists():
                try:
                    prev = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    prev = {}
            prev.update({
                "slug": e["slug"], "uuid": e["uuid"],
                "title": prev.get("title") or e["title"],
                "status": "needs-review",
                "failure_class": "process-failed",
                "last_failed_at": now.isoformat(),
                "review_reason": (f"自動再試行{MAX_ATTEMPTS}回失敗のため自動投入を停止。"
                                  "再開する場合は retries を0に戻し status を削除する"),
            })
            p.write_text(json.dumps(prev, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            paths.append(p)
            print(f"needs-review: {e['slug']}/{e['uuid']} {e['title']}")
        commit_state(paths, f"state: needs-review {len(paths)} vod(s)", fatal=False)
    elif exhausted:
        for e in exhausted:
            print(f"[dry-run] needs-review: {e['slug']}/{e['uuid']} {e['title']}")

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

    # ショート候補の下書き。投稿しない別軸なので、ここが失敗してもアーカイブ本線は
    # 止めない (check=False)。stateも持たない = 取りこぼしても次の配信で作り直せる
    shorts_n = int(cfg.get("shorts_n", 8))
    for c in shorts_targets(picks, cfg):
        url = f"https://kick.com/{c['slug']}/videos/{c['uuid']}"
        r = run(["gh", "workflow", "run", "shorts_prep.yml",
                 "-f", f"video={url}", "-f", "platform=kick", "-f", f"n={shorts_n}"],
                check=False)
        state = "queued" if r.returncode == 0 else "SKIPPED (dispatch失敗)"
        print(f"shorts {state}: {c['slug']}/{c['uuid']} {c['title']}")


if __name__ == "__main__":
    main()
