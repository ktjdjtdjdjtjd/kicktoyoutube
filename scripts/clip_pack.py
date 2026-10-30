"""雑談切り抜き総集編の CapCut 用素材を transcripts/<uuid>.md から生成する。

  python scripts/clip_pack.py --uuid 2866728770 --out clips/2866728770_pack

出力:
  cutlist.csv          採用区間(元動画の in/out と、繋いだ後のタイムライン位置)
  chapters.csv         チャプター(タイムライン位置+見出し)
  compilation.srt      繋いだ後のタイムラインに合わせた字幕(全区間)
  chapters.srt         チャプター見出しを区間全体に載せた字幕(チャプターテンプレ差し替え用)
  segments/NN_*.srt    区間ごと(区間の先頭=00:00:00)の字幕
"""
import argparse
import csv
import re
from pathlib import Path

# 文字起こし(whisper small)の定番誤認識を字幕向けに直す
REPLACE = [
    (r"オーバー(ボッチ|モチ|ホッチ|落ち|ボッジ|ウオッチ|ウォッチェ|ウォッチェー|ボチ|ウォッチェ)", "オーバーウォッチ"),
    (r"(ペンク|ヘンク|エング|ヘンゲ|テング|クェーク)ちゃん", "天狗ちゃん"),
    (r"プレミアムカラビ", "プレミアムカルビ"),
    (r"ジャンクケラー", "ジャンクラ"),
    (r"死ぬば", "シルバー"),
    (r"シルバ(?![ー])", "シルバー"),
    (r"ブロンゼ", "ブロンズ"),
    (r"うれション|ウレション|ウレッション", "うれション"),
]
MAX_CHARS = 26   # 1キューの目安文字数
MAX_DUR = 7.0    # 1キューの最長秒数
MIN_DUR = 0.9


def hms_to_sec(s):
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def sec_to_srt(t):
    t = max(0.0, t)
    ms = int(round((t - int(t)) * 1000))
    s = int(t)
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}"


def clean(text):
    for pat, rep in REPLACE:
        text = re.sub(pat, rep, text)
    return text.strip()


def split_chunks(text):
    """句読点で区切り、MAX_CHARS 前後の塊にまとめる。"""
    parts = [p for p in re.split(r"(?<=[、。！？!?,])", text) if p.strip()]
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > MAX_CHARS:
            out.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    # 句読点が無い長文は文字数で機械的に分割
    final = []
    for c in out:
        while len(c) > MAX_CHARS * 1.5:
            final.append(c[:MAX_CHARS])
            c = c[MAX_CHARS:]
        final.append(c)
    return [re.sub(r"[、,]$", "", c).strip() for c in final if c.strip()]


def load_transcript(path):
    lines = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(\d\d:\d\d:\d\d)\]\s*(.*)$", ln)
        if m:
            lines.append((hms_to_sec(m.group(1)), m.group(2)))
    return lines


def cues_for_segment(lines, start, end):
    """区間内の文字起こし行 → (abs_start, abs_end, text) のキュー列。"""
    idx = [i for i, (t, _) in enumerate(lines) if start <= t < end]
    # 1〜2秒の単語行が続くので、短い行は繋げて読める長さにする
    merged = []
    for k, i in enumerate(idx):
        t, txt = lines[i]
        txt = clean(txt)
        if not txt:
            continue
        nxt = lines[idx[k + 1]][0] if k + 1 < len(idx) else end
        if (merged and len(merged[-1][1]) + len(txt) + 1 <= MAX_CHARS
                and t - merged[-1][2] <= 1.5 and nxt - merged[-1][0] <= 6.0):
            merged[-1] = (merged[-1][0], merged[-1][1] + " " + txt, nxt)
        else:
            merged.append((t, txt, nxt))
    cues = []
    for t, txt, nxt in merged:
        dur = min(max(nxt - t, MIN_DUR), MAX_DUR)
        chunks = split_chunks(txt)
        total = sum(len(c) for c in chunks) or 1
        pos = t
        for c in chunks:
            d = max(MIN_DUR, dur * len(c) / total)
            cend = min(pos + d, end)
            if cend - pos < 0.3:
                break
            cues.append((pos, cend, c))
            pos = cend
    # 重なり除去
    fixed = []
    for s, e, c in cues:
        if fixed and s < fixed[-1][1]:
            fixed[-1] = (fixed[-1][0], s, fixed[-1][2])
        fixed.append((s, e, c))
    return [x for x in fixed if x[1] - x[0] >= 0.3]


def write_srt(path, cues):
    with open(path, "w", encoding="utf-8") as f:
        for n, (s, e, c) in enumerate(cues, 1):
            f.write(f"{n}\n{sec_to_srt(s)} --> {sec_to_srt(e)}\n{c}\n\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uuid", required=True)
    ap.add_argument("--segments", required=True, help="CSV: order,start,end,title")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    lines = load_transcript(f"transcripts/{a.uuid}.md")
    segs = []
    with open(a.segments, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            segs.append((int(row["order"]), hms_to_sec(row["start"]),
                         hms_to_sec(row["end"]), row["title"]))
    segs.sort()

    out = Path(a.out)
    (out / "segments").mkdir(parents=True, exist_ok=True)

    tl = 0.0
    comp, chap, cut_rows, chap_rows = [], [], [], []
    for order, s, e, title in segs:
        dur = e - s
        cues = cues_for_segment(lines, s, e)
        # 区間単体SRT (区間先頭=0)
        write_srt(out / "segments" / f"{order:02d}_{title[:12]}.srt",
                  [(cs - s, ce - s, c) for cs, ce, c in cues])
        # 総集編タイムラインSRT
        comp += [(tl + (cs - s), tl + (ce - s), c) for cs, ce, c in cues]
        chap.append((tl, tl + dur, title))
        cut_rows.append({
            "order": order, "src_in": sec_to_srt(s)[:-4], "src_out": sec_to_srt(e)[:-4],
            "duration": sec_to_srt(dur)[:-4], "timeline_in": sec_to_srt(tl)[:-4],
            "timeline_out": sec_to_srt(tl + dur)[:-4], "title": title, "cues": len(cues)})
        chap_rows.append({"order": order, "timeline_in": sec_to_srt(tl)[:-4],
                          "timeline_out": sec_to_srt(tl + dur)[:-4], "title": title})
        tl += dur

    write_srt(out / "compilation.srt", comp)
    write_srt(out / "chapters.srt", chap)
    for name, rows in (("cutlist.csv", cut_rows), ("chapters.csv", chap_rows)):
        with open(out / name, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"segments={len(segs)} total={sec_to_srt(tl)[:-4]} cues={len(comp)} -> {out}")


if __name__ == "__main__":
    main()
