"""Record live viewer counts for Top10 scoring via Kick's OFFICIAL public API (api.kick.com/public/v1).

Auth: app access token (OAuth client_credentials) from secrets KICK_CLIENT_ID / KICK_CLIENT_SECRET.
The internal kick.com/api/v2 is not used here (unofficial).

Storage (append-only, conflict-free): state/viewers/live-<start>/<run_id>.jsonl, one sample per line:
  {"at": "<real UTC ISO>", "viewers": int, "start_time": "<UTC ISO>"}
Linking (--link-meta) merges every run file of the session, dedupes per wall-clock minute,
computes t = at - start_time, and records gaps > GAP_MAX_SEC as gaps (no interpolation).
"""
import argparse, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

TOKEN_URL = "https://id.kick.com/oauth/token"
API = "https://api.kick.com/public/v1"
GAP_MAX_SEC = 180


def parse_ts(v):
    v = str(v).replace(" ", "T")
    if not (v.endswith("Z") or "+" in v[10:]):
        v += "+00:00"
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)


def app_token(cid, secret):
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]


def fetch_stream(token, slug):
    req = urllib.request.Request(f"{API}/channels?slug={urllib.parse.quote(slug)}", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r).get("data") or []
    st = (data[0].get("stream") or {}) if data else {}
    if not st.get("is_live"):
        return None
    return {"viewers": int(st.get("viewer_count") or 0), "start_time": parse_ts(st["start_time"]).isoformat()}


def session_dir(base, start_time):
    return os.path.join(base, "live-" + parse_ts(start_time).strftime("%Y%m%dT%H%M%SZ"))


def append(base, run_id, s, now):
    d = session_dir(base, s["start_time"])
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{run_id}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now.isoformat(), "viewers": s["viewers"], "start_time": s["start_time"]}) + "\n")


def build_series(rows, start_time, gap_max=GAP_MAX_SEC):
    """rows: [{at, viewers}] from any number of runs (dupes/out-of-order ok). Returns {samples, gaps}."""
    t0 = parse_ts(start_time)
    by_min = {}
    for r in rows:
        at = parse_ts(r["at"])
        key = int(at.timestamp() // 60)
        if key not in by_min or at < parse_ts(by_min[key]["at"]):
            by_min[key] = r
    samples = sorted(({"t": round((parse_ts(r["at"]) - t0).total_seconds()), "viewers": int(r["viewers"]), "at": parse_ts(r["at"]).isoformat()}
                      for r in by_min.values()), key=lambda x: x["t"])
    samples = [s for s in samples if s["t"] >= 0]
    gaps = [{"from": a["t"], "to": b["t"], "seconds": b["t"] - a["t"]} for a, b in zip(samples, samples[1:]) if b["t"] - a["t"] > gap_max]
    if samples and samples[0]["t"] > gap_max:
        gaps.insert(0, {"from": 0, "to": samples[0]["t"], "seconds": samples[0]["t"]})
    return {"start_time": t0.isoformat(), "samples": samples, "gaps": gaps}


def link_session(base, vod_start, tol=600):
    if not os.path.isdir(base):
        return None
    v0 = parse_ts(vod_start)
    for n in sorted(os.listdir(base)):
        p = os.path.join(base, n)
        if not (n.startswith("live-") and os.path.isdir(p)):
            continue
        rows = []
        for fn in sorted(os.listdir(p)):
            if fn.endswith(".jsonl"):
                with open(os.path.join(p, fn), encoding="utf-8") as f:
                    rows += [json.loads(l) for l in f if l.strip()]
        if rows and abs((parse_ts(rows[0]["start_time"]) - v0).total_seconds()) <= tol:
            # series uses the VOD start so t matches VOD seconds
            return build_series(rows, vod_start)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="zingisukan2525")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--duration", type=int, default=290, help="sample loop length; covers the 5-min cron")
    ap.add_argument("--dir", default="state/viewers")
    ap.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
    ap.add_argument("--link-meta")
    ap.add_argument("--link-out")
    a = ap.parse_args()
    if a.link_meta:
        found = link_session(a.dir, json.load(open(a.link_meta, encoding="utf-8"))["start_time"])
        if not found:
            print("no viewer samples for this VOD"); raise SystemExit(1)
        with open(a.link_out, "w", encoding="utf-8") as f:
            json.dump(found, f)
        print(f"linked {len(found['samples'])} samples, {len(found['gaps'])} gaps")
        return
    cid, sec = os.environ.get("KICK_CLIENT_ID", ""), os.environ.get("KICK_CLIENT_SECRET", "")
    if not cid or not sec:
        print("KICK_CLIENT_ID/KICK_CLIENT_SECRET not set; skipping"); return
    token = app_token(cid, sec)
    end = time.monotonic() + a.duration
    while True:
        try:
            s = fetch_stream(token, a.slug)
        except Exception as e:  # one failed sample is a gap, not a crash; no tight retry
            print("sample failed:", type(e).__name__); s = False
        if s is None:
            print("offline"); return
        if s:
            append(a.dir, a.run_id, s, datetime.now(timezone.utc))
            print("viewers", s["viewers"])
        if time.monotonic() + a.interval > end:
            return
        time.sleep(a.interval - (time.time() % a.interval))  # align to wall-clock minute


if __name__ == "__main__":
    sys.exit(main())
