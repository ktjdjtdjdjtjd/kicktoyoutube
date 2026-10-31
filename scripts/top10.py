"""Top10 excitement segment picker for Kick VODs (see bridgeclip docs/REQUIREMENTS_top10.md).

Input: chat.jsonl ({"rel": sec, "content": str, optional "user"}),
optional viewers.json ([{"t": sec, "viewers": int}] or {"samples":[...]}), optional raids.json ([{"t": sec}]).
Output: top10.json with segments, scores, reasons, raid decisions.
"""
import argparse, json, math, re, statistics

BIN = 10
W_CHAT, W_WW, W_VIEW = 0.6, 0.15, 0.25
PER_USER_CAP = 3
PAD_PRE, PAD_POST, MAX_LEN, MIN_GAP = 30, 20, 300, 60
RAID_JUMP_RATIO, RAID_JUMP_MIN, RAID_WINDOW = 0.30, 50, 60
RAID_HALF_LIFE = 600
HYPE_CHAT_RATIO = 2.0
LAUGH = re.compile(r"[wｗ]{2,}|笑{2,}|草", re.I)
RAID_MSG = re.compile(r"raid|レイド|hosted|ホスト", re.I)


def robust_z(xs):
    if not xs:
        return []
    med = statistics.median(xs)
    mad = statistics.median([abs(x - med) for x in xs]) * 1.4826
    if mad <= 1e-9:
        sd = statistics.pstdev(xs)
        mad = sd if sd > 1e-9 else 1.0
    return [(x - med) / mad for x in xs]


def smooth(xs, k=3):
    out = []
    for i in range(len(xs)):
        lo, hi = max(0, i - k // 2), min(len(xs), i + k // 2 + 1)
        out.append(sum(xs[lo:hi]) / (hi - lo))
    return out


def chat_bins(messages, duration):
    n = max(1, math.ceil(duration / BIN))
    chat, ww, per_user = [0] * n, [0] * n, {}
    raid_msgs = []
    for m in messages:
        t = float(m.get("rel", m.get("seconds", -1)))
        if t < 0 or t >= duration:
            continue
        b = int(t // BIN)
        text = str(m.get("content", m.get("text", "")))
        if m.get("type") not in (None, "message") and RAID_MSG.search(text):
            raid_msgs.append(t)
            continue
        user = m.get("user")
        if user:
            key = (b, user)
            per_user[key] = per_user.get(key, 0) + 1
            if per_user[key] > PER_USER_CAP:
                continue
        chat[b] += 1
        if LAUGH.search(text):
            ww[b] += 1
    return chat, ww, raid_msgs


VIEW_GAP_MAX = 180  # 2サンプルの間隔がこれを超えたら補間せず「不明」


def viewer_bins(samples, n, gaps=None):
    """Per-bin viewers, or None where unknown. Only interpolates between samples <= VIEW_GAP_MAX apart;
    outside that (cron skip/delay, recorder down, before first/after last sample) the bin is None."""
    if not samples:
        return None
    pts = sorted({float(s["t"]): float(s["viewers"]) for s in samples}.items())
    out, j = [], 0
    for i in range(n):
        t = i * BIN + BIN / 2
        while j + 1 < len(pts) and pts[j + 1][0] <= t:
            j += 1
        v = None
        if pts[j][0] <= t and j + 1 < len(pts) and pts[j + 1][0] - pts[j][0] <= VIEW_GAP_MAX:
            (t0, v0), (t1, v1) = pts[j], pts[j + 1]
            v = v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        else:
            near = min(pts, key=lambda p: abs(p[0] - t)) if len(pts) < 3 else min(pts[max(0, j - 1):j + 2], key=lambda p: abs(p[0] - t))
            if abs(near[0] - t) <= VIEW_GAP_MAX / 2:
                v = near[1]
        for g in gaps or []:
            if float(g["from"]) < t < float(g["to"]):
                v = None
        out.append(v)
    return out if any(v is not None for v in out) else None


def detect_raids(viewers, chat, raid_msg_times):
    """Return list of {bin, size, source, hype}. Step jump within RAID_WINDOW without matching chat rise, or a raid system message."""
    raids = []
    w = max(1, RAID_WINDOW // BIN)
    base_chat = statistics.median(chat) if chat else 0
    cand = {int(t // BIN): "chat_system_message" for t in raid_msg_times}
    if viewers:
        i = w
        while i < len(viewers):
            before, after = viewers[i - w], viewers[i]
            if before is None or after is None:
                i += 1; continue
            if before > 0 and after - before >= max(RAID_JUMP_MIN, before * RAID_JUMP_RATIO):
                cand.setdefault(i, "viewer_step")
                i += w * 3
            else:
                i += 1
    for b, src in sorted(cand.items()):
        size = 0.0
        if viewers:
            w0 = max(0, b - w)
            win = [v for v in viewers[b:b + w + 1] if v is not None]
            size = max(0.0, max(win) - viewers[w0]) if win and viewers[w0] is not None else 0.0
        pre = chat[max(0, b - 18):b] or [base_chat]
        post = chat[b:b + 18] or [0]
        pre_rate, post_rate = sum(pre) / len(pre), sum(post) / len(post)
        chat_ratio = post_rate / max(pre_rate, base_chat, 0.5)
        hype = chat_ratio >= HYPE_CHAT_RATIO
        win = [v for v in (viewers or [])[b:b + w + 1] if v is not None]
        if viewers and hype and win and viewers[max(0, b - w)] is not None:
            # comments per viewer must not collapse (pure raid chat spam is diluted by new viewers)
            vb, va = viewers[max(0, b - w)], max(win)
            cpv_before, cpv_after = pre_rate / max(vb, 1), post_rate / max(va, 1)
            hype = cpv_after >= 0.5 * cpv_before
        if src == "viewer_step" and not hype and chat_ratio >= 1.3 and size < 0:
            continue
        raids.append({"bin": b, "t": b * BIN, "size": round(size), "source": src,
                      "chat_ratio": round(chat_ratio, 2), "decision": "keep_chat_hype" if hype else "subtract"})
    return raids


def adjust_viewers(viewers, raids):
    if not viewers:
        return None
    adj = list(viewers)
    for r in raids:
        for i in range(r["bin"], len(adj)):
            dt = (i - r["bin"]) * BIN
            if adj[i] is not None:
                adj[i] -= r["size"] * 0.5 ** (dt / RAID_HALF_LIFE)
    return [None if v is None else max(0.0, v) for v in adj]


def pick_segments(score, duration, k=10):
    order = sorted(range(len(score)), key=lambda i: -score[i])
    taken = []
    thr = statistics.median(score) + 0.5 * (statistics.pstdev(score) if len(score) > 1 else 0)
    for peak in order:
        if len(taken) >= k:
            break
        if score[peak] <= thr and taken:
            pass
        ps = peak * BIN
        if any(ps >= s - MIN_GAP and ps <= e + MIN_GAP for s, e, _ in taken):
            continue
        lo = hi = peak
        while lo - 1 >= 0 and score[lo - 1] > thr and (hi - lo + 2) * BIN <= MAX_LEN - PAD_PRE - PAD_POST:
            lo -= 1
        while hi + 1 < len(score) and score[hi + 1] > thr and (hi - lo + 2) * BIN <= MAX_LEN - PAD_PRE - PAD_POST:
            hi += 1
        s = max(0, lo * BIN - PAD_PRE)
        e = min(duration, (hi + 1) * BIN + PAD_POST)
        if e - s > MAX_LEN:
            e = s + MAX_LEN
        # clip to neighbours' gap
        bad = False
        for ts, te, _ in taken:
            if s < te + MIN_GAP and e > ts - MIN_GAP:
                if ts >= peak * BIN:
                    e = min(e, ts - MIN_GAP)
                else:
                    s = max(s, te + MIN_GAP)
                if e - s < 30:
                    bad = True
        if not bad:
            taken.append((s, e, peak))
    return taken


def analyze(messages, duration, viewer_samples=None, raid_events=None, k=10, viewer_gaps=None):
    chat, ww, raid_msgs = chat_bins(messages, duration)
    raid_msgs += [float(r["t"]) for r in (raid_events or [])]
    n = len(chat)
    viewers = viewer_bins(viewer_samples, n, viewer_gaps)
    raids = detect_raids(viewers, chat, raid_msgs)
    vadj = adjust_viewers(viewers, raids)
    zc, zw = robust_z(chat), robust_z(ww)
    zv = [None] * n
    if vadj:
        known = [i for i, v in enumerate(vadj) if v is not None]
        for i, z in zip(known, robust_z([vadj[i] for i in known])):
            zv[i] = z
    # 視聴者不明のビンは重みをチャット側へ再正規化（0扱いにしない）
    raw = [(W_CHAT * a + W_WW * b) / (W_CHAT + W_WW) if c is None else W_CHAT * a + W_WW * b + W_VIEW * c
           for a, b, c in zip(zc, zw, zv)]
    score = smooth(raw, 3)
    base_rate = statistics.median(chat) * 6 if chat else 0
    segs = []
    for rank, (s, e, peak) in enumerate(sorted(pick_segments(score, duration, k), key=lambda x: -score[x[2]]), 1):
        b0, b1 = int(s // BIN), max(int(s // BIN) + 1, math.ceil(e / BIN))
        c = sum(chat[b0:b1]); w = sum(ww[b0:b1])
        mins = (e - s) / 60
        peak_min = sum(chat[peak:peak + 6])
        overl = [r for r in raids if s - RAID_WINDOW <= r["t"] <= e]
        reason = f"ピーク毎分{peak_min}件（平時{base_rate:.0f}件の{peak_min / max(base_rate, 1):.1f}倍）・区間{c}件・笑い{w}件（{(w / max(c, 1)) * 100:.0f}%）"
        if overl:
            reason += " ・レイド重なり:" + ",".join("コメントも急増→採用" if r["decision"] == "keep_chat_hype" else "視聴者増は差引" for r in overl)
        segs.append({"rank": rank, "start": round(s, 1), "end": round(e, 1), "length": round(e - s, 1),
                     "peakAt": peak * BIN, "score": round(score[peak], 3), "commentCount": c, "laughCount": w,
                     "laughRate": round(w / max(c, 1), 3), "peakPerMinute": peak_min,
                     "perMinute": round(c / max(mins, 1e-9), 1), "raids": overl, "reason": reason})
    return {"schemaVersion": 1, "durationSeconds": duration, "binSeconds": BIN,
            "viewerData": bool(viewers),
            "viewerCoverage": round(sum(v is not None for v in viewers) / n, 3) if viewers else 0.0, "raids": raids, "baselinePerMinute": base_rate, "segments": segs}


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--viewers")
    ap.add_argument("--raids")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out", default="top10.json")
    ap.add_argument("--matrix-out", help="write GitHub Actions matrix json")
    a = ap.parse_args()
    vs, gaps = None, None
    if a.viewers:
        d = json.load(open(a.viewers, encoding="utf-8"))
        vs = d["samples"] if isinstance(d, dict) else d
        gaps = d.get("gaps") if isinstance(d, dict) else None
    rs = json.load(open(a.raids, encoding="utf-8")) if a.raids else None
    res = analyze(load_jsonl(a.chat), a.duration, vs, rs, a.k, gaps)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    if a.matrix_out:
        with open(a.matrix_out, "w") as f:
            json.dump([{"idx": s["rank"], "start": s["start"], "end": s["end"]} for s in res["segments"]], f)
    for s in res["segments"]:
        print(s["rank"], s["start"], s["end"], s["reason"])


if __name__ == "__main__":
    main()
