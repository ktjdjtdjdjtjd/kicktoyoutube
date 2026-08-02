"""full.ass からセグメント区間 [start, end) を切り出し、時刻を -start シフトした ASS を書く。

セグメント境界をまたぐダンマク (開始が start より前) は、境界時点の X 座標を
線形補間で求めて \\move の始点に据え直すので、結合後も流れが途切れない。

    python slice_ass.py <full.ass> <seg_start_s> <seg_end_s> <out.ass>
"""
import re
import sys

DIALOGUE_RE = re.compile(
    r"^Dialogue: (\d+),([\d:.]+),([\d:.]+),(.*?\\move\()"
    r"(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)"
    r"(\).*)$"
)


def parse_time(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def fmt_time(seconds):
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def slice_ass(lines, seg_start, seg_end):
    out = []
    n_events = 0
    for line in lines:
        if not line.startswith("Dialogue:"):
            out.append(line)
            continue
        m = DIALOGUE_RE.match(line)
        if not m:
            continue
        layer, t0, t1, pre, x1, y1, x2, y2, post = m.groups()
        ev_start = parse_time(t0)
        ev_end = parse_time(t1)
        if ev_end <= seg_start or ev_start >= seg_end:
            continue
        x1f, x2f = float(x1), float(x2)
        new_start = ev_start - seg_start
        new_end = ev_end - seg_start
        if new_start < 0:
            # 境界時点の位置を補間して始点に据え直す (速度は同一のまま継続)
            dur = ev_end - ev_start
            if dur <= 0:
                continue
            frac = (seg_start - ev_start) / dur
            x1f = x1f + (x2f - x1f) * frac
            new_start = 0.0
        if new_end - new_start < 0.05:
            continue
        out.append(
            f"Dialogue: {layer},{fmt_time(new_start)},{fmt_time(new_end)},{pre}"
            f"{x1f:.0f},{y1},{x2f:.0f},{y2}{post}"
        )
        n_events += 1
    return out, n_events


def main():
    if len(sys.argv) != 5:
        sys.exit("usage: python slice_ass.py <full.ass> <seg_start_s> <seg_end_s> <out.ass>")
    src, seg_start, seg_end, dst = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    with open(src, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    out, n = slice_ass(lines, seg_start, seg_end)
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"sliced {n} events -> {dst}", file=sys.stderr)


if __name__ == "__main__":
    main()
