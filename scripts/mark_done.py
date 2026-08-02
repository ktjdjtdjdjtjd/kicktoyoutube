"""state/<uuid>.json を更新してコミットする (process 完了/失敗の記録)。

    python mark_done.py <uuid> --status done --yt-url https://...
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from watch import commit_state, STATE_DIR


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uuid")
    ap.add_argument("--status", default="done")
    ap.add_argument("--yt-url", default="")
    a = ap.parse_args()
    STATE_DIR.mkdir(exist_ok=True)
    p = STATE_DIR / f"{a.uuid}.json"
    data = {}
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    data["status"] = a.status
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    if a.yt_url:
        data["yt_url"] = a.yt_url
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if not commit_state([p], f"state: {a.uuid} -> {a.status}"):
        raise SystemExit("error: could not push state")


if __name__ == "__main__":
    main()
