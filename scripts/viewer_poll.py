"""Poll Kick channel info and append live viewer counts to state/viewers/<session>.json.
Kick VOD uuid is not known while live, so samples are keyed by livestream id and start time;
top10 workflow links them by start_time (see link_session)."""
import argparse, json, os, time
from datetime import datetime, timezone
try:
    import kick_api
except ImportError:  # link mode only needs stdlib
    kick_api = None


def sample(slug):
    ch = kick_api.get_channel(slug)
    live = ch.get("livestream") or None
    if not live or not live.get("is_live", True):
        return None
    return {"id": live.get("id"), "start_time": live.get("start_time") or live.get("created_at"),
            "viewers": int(live.get("viewer_count") or 0)}


def append(dirpath, s, now):
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, f"live-{s['id']}.json")
    d = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"livestreamId": s["id"], "start_time": s["start_time"], "samples": []}
    start = kick_api_parse(s["start_time"]) if s["start_time"] else now
    d["samples"].append({"t": round((now - start).total_seconds()), "viewers": s["viewers"], "at": now.isoformat()})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return path


def kick_api_parse(v):
    v = str(v).replace(" ", "T")
    if not (v.endswith("Z") or "+" in v[10:]):
        v += "+00:00"
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def link_session(dirpath, vod_start, tol=600):
    if not os.path.isdir(dirpath):
        return None
    t0 = kick_api_parse(vod_start)
    for n in sorted(os.listdir(dirpath)):
        if not n.startswith("live-"):
            continue
        d = json.load(open(os.path.join(dirpath, n), encoding="utf-8"))
        if d.get("start_time") and abs((kick_api_parse(d["start_time"]) - t0).total_seconds()) <= tol:
            return d
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="zingisukan2525")
    ap.add_argument("--samples", type=int, default=4)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--dir", default="state/viewers")
    ap.add_argument("--link-meta")
    ap.add_argument("--link-out")
    a = ap.parse_args()
    if a.link_meta:
        found = link_session(a.dir, json.load(open(a.link_meta, encoding="utf-8"))["start_time"])
        if not found:
            print("no viewer samples for this VOD"); raise SystemExit(1)
        with open(a.link_out, "w", encoding="utf-8") as f:
            json.dump(found, f)
        return
    for i in range(a.samples):
        s = sample(a.slug)
        if s is None:
            print("offline"); return
        print("viewers", s["viewers"], append(a.dir, s, datetime.now(timezone.utc)))
        if i + 1 < a.samples:
            time.sleep(a.interval)


if __name__ == "__main__":
    main()
