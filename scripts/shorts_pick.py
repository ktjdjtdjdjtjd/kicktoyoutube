"""配信のチャットから候補区間を選び、shorts_prep の segments を自動で埋める。

request.json に segments が無いときだけ動く（手で区間を指定した依頼はそのまま通す）。

選定ロジックは手元の候補ボード(combo_short/candidate_board.py)からの移植で、
スコアや閾値は一切変えていない。片方だけ直すと朝ボードに並ぶ候補とクラウドが
選ぶ候補がズレるため、直すときは両方に同じ変更を入れる。

  python scripts/shorts_pick.py [--n 8] [--max-sec 70] [--min-sec 30]
"""
import argparse
import json
import re
import sys
from pathlib import Path

import chat_fetch
import kick_api
from burn_request import KICK_URL_RE, TWITCH_URL_RE

# ---- ここから candidate_board.py と同一（移植・ロジック変更禁止） --------
EMOTES = {
    "laugh": re.compile(r"[wｗ]{2,}|草|ワロタ|わろた|クソワロ|笑"),
    "wow":   re.compile(r"すご|すげ|やば|神|えぐ|上手|うま|ナイス|強い|天才"),
    "cute":  re.compile(r"かわい|かわよ|尊い|てぇてぇ|きゃわ|癒され"),
    "shock": re.compile(r"!\?|！？|えぇ|ええ…|は？|マジかよ|うそだろ|ヒェ|怖|こわ|悲鳴"),
}
EMO_W = {"laugh": 3, "wow": 2, "cute": 2, "shock": 2}
EMO_JP = {"laugh": "🤣爆笑", "wow": "😲すごい", "cute": "💖かわいい", "shock": "❗衝撃"}


def emo_of(text):
    return {k for k, rx in EMOTES.items() if rx.search(text or "")}


def rolling_median(scored, b, win, span=10):
    vals = [scored[x] for x in range(b - span * win, b + span * win + 1, win)
            if x in scored and x != b]
    if len(vals) < 3:
        vals = sorted(scored.values())
    vals = sorted(vals)
    return vals[len(vals) // 2]


def find_candidates(rows, win=30, topn=20, min_sc=6):
    """チャット[(t,text)]から候補区間を抽出。順位は平常比(rel)=周辺と比べた跳ね方。"""
    if not rows:
        return []
    buckets = {}
    for t, txt in rows:
        b = int(t // win) * win
        d = buckets.setdefault(b, {"n": 0, "laugh": 0, "wow": 0, "cute": 0, "shock": 0})
        d["n"] += 1
        for k in emo_of(txt):
            d[k] += 1
    scored = {b: v["n"] + sum(EMO_W[k] * v[k] for k in EMO_W) for b, v in buckets.items()}
    rel = {b: s / (rolling_median(scored, b, win) + 3) for b, s in scored.items()}
    seeds = sorted(scored.items(), key=lambda kv: -rel[kv[0]])
    used, out = set(), []
    for b, sc in seeds:
        if b in used or sc < min_sc:
            continue
        a = e = b
        while (a - win) in scored and rel.get(a - win, 0) >= 1.6 and (a - win) not in used:
            a -= win
        while (e + win) in scored and rel.get(e + win, 0) >= 1.6 and (e + win) not in used:
            e += win
        for x in range(a, e + win, win):
            used.add(x)
        agg = {"n": 0, "laugh": 0, "wow": 0, "cute": 0, "shock": 0}
        for x in range(a, e + win, win):
            if x in buckets:
                for k in agg:
                    agg[k] += buckets[x][k]
        out.append({"start": max(0, a - 10), "end": e + win + 10,
                    "score": agg["n"] + sum(EMO_W[k] * agg[k] for k in EMO_W),
                    "rel": round(max(rel.get(x, 0) for x in range(a, e + win, win)), 1),
                    "msgs": agg["n"], "laughs": agg["laugh"],
                    "cats": {k: agg[k] for k in EMO_W},
                    "tags": [EMO_JP[k] for k in EMO_W if agg[k] >= 3]})
        if len(out) >= topn * 2:
            break
    out.sort(key=lambda c: c["start"])
    merged = []
    for c in out:
        if merged and c["start"] <= merged[-1]["end"] + 5:
            m = merged[-1]
            m["end"] = max(m["end"], c["end"])
            m["msgs"] += c["msgs"]
            m["laughs"] += c["laughs"]
            for k in EMO_W:
                m["cats"][k] += c["cats"][k]
            m["score"] = m["msgs"] + sum(EMO_W[k] * m["cats"][k] for k in EMO_W)
            m["rel"] = max(m["rel"], c["rel"])
            m["tags"] = [EMO_JP[k] for k in EMO_W if m["cats"][k] >= 3]
        else:
            merged.append(c)
    merged.sort(key=lambda c: (-c["rel"], -c["score"]))
    return merged[:topn]


def top_comments(rows, a, b, n=6):
    """区間内の代表コメント(感情系優先・重複除去)。"""
    seen, emo, other = set(), [], []
    for t, txt in rows:
        if not (a <= t <= b):
            continue
        key = txt[:20]
        if key in seen or not txt.strip():
            continue
        seen.add(key)
        (emo if emo_of(txt) else other).append(txt[:48])
    return (emo + other)[:n]
# ---- 移植ここまで -------------------------------------------------------


def tighten(rows, start, end, max_sec, min_sec, step=5):
    """候補をショートの尺に詰める。

    候補ボードは「盛り上がったシーン」を可変長で返すので、そのままだと200秒級が
    混ざる(実測: 上位2件が200秒)。長いものはチャットが最も濃い窓へ寄せ、短すぎる
    ものは中心から広げる。人が朝に境界を直す前提なので、ここは荒く決めてよい。
    """
    start, end = float(start), float(end)
    if end - start > max_sec:
        best_n, best_s = -1, start
        s = start
        while s <= end - max_sec:
            n = sum(1 for t, _ in rows if s <= t < s + max_sec)
            if n > best_n:
                best_n, best_s = n, s
            s += step
        start, end = best_s, best_s + max_sec
    elif end - start < min_sec:
        c = (start + end) / 2.0
        start, end = c - min_sec / 2.0, c + min_sec / 2.0
    start = max(0.0, start)
    return round(start, 1), round(end, 1)


def to_segments(rows, cands, n, max_sec, min_sec):
    """候補 -> shorts_prep が食える segments。詰めた結果かぶったものは落とす。"""
    segs = []
    for c in cands:
        s, e = tighten(rows, c["start"], c["end"], max_sec, min_sec)
        if any(min(e, o["end"]) - max(s, o["start"]) > 3 for o in segs):
            continue
        segs.append({
            "id": len(segs) + 1, "start": s, "end": e,
            "score": c["score"], "rel": c["rel"], "msgs": c["msgs"],
            "tags": c["tags"], "comments": top_comments(rows, s, e),
        })
        if len(segs) >= n:
            break
    return segs


def fetch_rows(platform, video, workers=8):
    """[(rel_sec, text)] を返す。取得の作法は burn_request.py と同じ。"""
    if platform == "kick":
        m = KICK_URL_RE.search(video)
        if not m:
            sys.exit("error: kick URL が解釈できない: " + str(video))
        from plan import resolve_meta
        meta = resolve_meta(m.group(1), m.group(2))
        if meta.get("is_live"):
            sys.exit("error: VOD is still live")
        start_dt = chat_fetch.parse_dt(meta["start_time"])
        return chat_fetch.fetch_all_chat(meta["channel_id"], start_dt, meta["duration_s"],
                                         workers=workers, keep_emotes=False)
    m = TWITCH_URL_RE.search(video)
    vid = m.group(1) if m else video.lstrip("v")
    if not vid.isdigit():
        sys.exit("error: twitch VOD id が解釈できない: " + str(video))
    from twitch_chat_fetch import fetch_comments
    return fetch_comments(vid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", default="queue_shorts/request.json")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--n", type=int, default=None, help="採る本数(既定8)")
    ap.add_argument("--max-sec", type=float, default=70.0)
    ap.add_argument("--min-sec", type=float, default=30.0)
    a = ap.parse_args()

    rp = Path(a.request)
    req = json.loads(rp.read_text(encoding="utf-8"))
    if req.get("segments"):
        print("segments 指定済み(" + str(len(req["segments"])) + "件) — 自動選定はしない")
        return

    kick_api.load_config(a.config)
    rows = fetch_rows(req["platform"], req["video"])
    print("chat rows: " + str(len(rows)), file=sys.stderr)
    if not rows:
        sys.exit("error: チャットが取れなかった (候補を選べない)")

    n = int(a.n or req.get("n") or 8)
    cands = find_candidates(rows, topn=max(n * 2, 20))
    segs = to_segments(rows, cands, n, a.max_sec, a.min_sec)
    if not segs:
        sys.exit("error: 候補が1件も出なかった")
    req["segments"] = segs
    rp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    for s in segs:
        t = int(s["start"])
        print("  #%d %d:%02d:%02d %ds rel%s score%s %s"
              % (s["id"], t // 3600, t % 3600 // 60, t % 60,
                 int(s["end"] - s["start"]), s["rel"], s["score"], "".join(s["tags"])),
              file=sys.stderr)
    print("picked " + str(len(segs)) + " segments")


if __name__ == "__main__":
    main()
