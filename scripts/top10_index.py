"""Build release index.json consumed by BridgeClip「準備済みから選ぶ」(top10)."""
import argparse, hashlib, json, os


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    for k in ("--top10", "--meta", "--clips", "--uuid", "--run-id", "--out"):
        ap.add_argument(k, required=True)
    ap.add_argument("--chat")
    a = ap.parse_args()
    t = json.load(open(a.top10, encoding="utf-8")); m = json.load(open(a.meta, encoding="utf-8"))
    chat = []
    if a.chat:
        with open(a.chat, encoding="utf-8") as f:
            chat = [json.loads(l) for l in f if l.strip()]
    clips = []
    for s in t["segments"]:
        f = f"clip_{s['rank']:02d}.mp4"; p = os.path.join(a.clips, f)
        if not os.path.exists(p):
            raise SystemExit(f"missing {f}")
        msgs = [{"id": f"{s['rank']}-{i}", "seconds": round(float(m["rel"]) - s["start"], 3), "user": m.get("user", ""), "text": m.get("content", "")}
                for i, m in enumerate(c for c in chat if s["start"] <= float(c["rel"]) < s["end"])]
        cf, bf = f"clip_{s['rank']:02d}-comments.json", f"clip_{s['rank']:02d}-candidates.json"
        with open(os.path.join(a.clips, cf), "w", encoding="utf-8") as o:
            json.dump({"kickId": a.uuid, "sourceStartSeconds": s["start"], "durationSeconds": s["length"], "messages": msgs}, o, ensure_ascii=False)
        with open(os.path.join(a.clips, bf), "w", encoding="utf-8") as o:
            json.dump({"cands": [{"start": 0, "end": s["length"], "score": s["score"], "rel": 1}]}, o)
        files = {n: {"bytes": os.path.getsize(os.path.join(a.clips, n)), "digest": sha(os.path.join(a.clips, n))} for n in (f, cf, bf)}
        clips.append({"rank": s["rank"], "file": f, "commentsFile": cf, "candidatesFile": bf, "files": files, "bytes": os.path.getsize(p), "digest": sha(p),
                      "sourceStartSeconds": s["start"], "durationSeconds": s["length"], "score": s["score"],
                      "commentCount": s["commentCount"], "laughCount": s["laughCount"], "reason": s["reason"],
                      "raids": s["raids"]})
    out = {"schemaVersion": 2, "kind": "top10", "kickId": a.uuid, "title": m.get("title", ""),
           "startedAt": m.get("start_time"), "runId": int(a.run_id), "viewerData": t["viewerData"],
           "raids": t["raids"], "clips": clips}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
