"""process の第3段: セグメント結合 + 機械検証。

    python assemble.py --meta out/meta.json --segdir segs/ --out final.mp4

検証: ストリーム存在 / pix_fmt=yuv420p / 尺が期待値±10s。NGなら exit 1。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def ffprobe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", default="out/meta.json")
    ap.add_argument("--segdir", default="segs")
    ap.add_argument("--out", default="final.mp4")
    ap.add_argument("--tolerance", type=float, default=10.0)
    a = ap.parse_args()
    meta = json.loads(Path(a.meta).read_text(encoding="utf-8"))
    expected = meta["duration_s"]

    segs = sorted(Path(a.segdir).rglob("seg_*.mp4"))
    if not segs:
        sys.exit("error: no segment files found")
    print(f"segments: {[s.name for s in segs]}", file=sys.stderr)
    if len(segs) != len(meta["segments"]):
        sys.exit(f"error: segment count mismatch: files={len(segs)} plan={len(meta['segments'])}")

    if len(segs) == 1:
        Path(segs[0]).replace(a.out)
    else:
        lst = Path("concat.txt")
        lst.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
             "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", a.out],
            check=True)

    info = ffprobe(a.out)
    vstreams = [s for s in info["streams"] if s["codec_type"] == "video"]
    astreams = [s for s in info["streams"] if s["codec_type"] == "audio"]
    dur = float(info["format"]["duration"])
    errs = []
    if not vstreams:
        errs.append("no video stream")
    elif vstreams[0].get("pix_fmt") != "yuv420p":
        errs.append(f"pix_fmt={vstreams[0].get('pix_fmt')} (expected yuv420p)")
    if not astreams:
        errs.append("no audio stream")
    if abs(dur - expected) > a.tolerance:
        errs.append(f"duration={dur:.1f}s expected={expected:.1f}s")
    if errs:
        for e in errs:
            print(f"VERIFY FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"VERIFY OK: duration={dur:.1f}s pix_fmt=yuv420p -> {a.out}")


if __name__ == "__main__":
    main()
