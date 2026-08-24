"""縦型ショートの下書きレンダー (shorts_prep.yml から実行)。

shorts_prep.py が出した clip_NN.mp4 + clip_NN.srt + タイトル案を、
1080x1920 の縦型に組み直して字幕とヘッダーを焼く。
朝ボードで「見た目の完成形」を見て採否を決めるためのアタリで、
採用したものは手元の CapCut で本番を作る（この出力を納品しない）。

家のスタイルは auto-shorts と揃える（同じ ASS スタイル・同じ改行規則）。
フォントだけはランナーに Keifont が無いため BIZ UDPGothic を使う。
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jp_wrap

CW, CH = 1080, 1920
FONT = "BIZ UDPGothic"          # ランナーで入手できる唯一の日本語フォント

# 折り返し: 字幕は 78px、左右マージン60+60なので実効幅960px = 全角12文字が上限。
# jp_wrap は max_lines を超えると per_line を破って詰め込む(=はみ出す)ため、
# 行数側を緩めて文字数側を必ず守らせる。実SRT 14,248セグメントの実測は
# 中央値7文字・90%が14文字なので、ほとんどは2行に収まる。
CAP_WRAP, CAP_LINES = 12, 3
TITLE_WRAP = 9                  # ヘッダーは1行7〜9文字(超えると左右にはみ出す)
SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d+)\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d+)")


ASS_NL = chr(92) + "N"


def wrap_safe(text, per_line, max_lines):
    """必ず per_line 以内に折る。

    jp_wrap は意味の切れ目を優先するので、句読点も空白も無い長い発話では
    per_line を破って詰め込む(実測: 51文字が17文字×3行)。はみ出した字は
    画面外に消えて後から気付けないため、最後は機械的に切って幅を保証する。
    """
    out = []
    for row in jp_wrap.wrap_jp(text, per_line, max_lines).split(ASS_NL):
        while len(row) > per_line:
            out.append(row[:per_line])
            row = row[per_line:]
        if row:
            out.append(row)
    return ASS_NL.join(out)

def sec_to_ass(t):
    t = max(0.0, float(t))
    h, m = int(t // 3600), int(t % 3600 // 60)
    s = t - h * 3600 - m * 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def esc(t):
    return t.replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def parse_srt(path):
    """SRT -> [{start,end,text}]。時刻はクリップ内の相対秒（whisperがクリップ単体を見ている）。"""
    segs = []
    text = Path(path).read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", text.strip())
    for b in blocks:
        m = SRT_TIME.search(b)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        body = " ".join(ln.strip() for ln in b.splitlines()[m.string[:m.start()].count("\n") + 1:])
        body = body.strip()
        if body:
            segs.append({"start": st, "end": en, "text": body})
    return segs


def build_ass(segs, title, clip_dur, ass_path):
    """auto_shorts.ass_header と同じスタイル定義（Cap=白・下/Title=黄・上）。"""
    head = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {CW}\nPlayResY: {CH}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{FONT},78,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&H64000000,1,0,0,0,100,100,0,0,1,5,2,2,60,60,210,1\n"
        f"Style: Title,{FONT},66,&H0000F0FF,&H0000F0FF,&H00000000,"
        "&H96000000,1,0,0,0,100,100,0,0,1,5,1,8,60,60,120,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [head]
    if title:
        lines.append(f"Dialogue: 0,{sec_to_ass(0)},{sec_to_ass(clip_dur)},Title,,0,0,0,,"
                     f"{esc(wrap_safe(title, TITLE_WRAP, 2))}")
    anim = r"{\fad(60,30)\fscx40\fscy40\t(0,90,\fscx113\fscy113)\t(90,180,\fscx100\fscy100)}"
    for s in segs:
        st, en = max(0.0, s["start"]), min(float(clip_dur), s["end"])
        if en - st < 0.2:
            continue
        lines.append(f"Dialogue: 0,{sec_to_ass(st)},{sec_to_ass(en)},Cap,,0,0,0,,"
                     f"{anim}{esc(wrap_safe(s['text'], CAP_WRAP, CAP_LINES))}")
    Path(ass_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def probe_dur(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


# blur レイアウト: 背景を引き伸ばしてぼかし、前景を横幅に合わせて中央へ。
# auto_shorts.vertical_chain の blur 分岐と同じ組み立て。
VCHAIN = (
    "[0:v]split=2[bg][fg];"
    "[bg]scale=216:384:force_original_aspect_ratio=increase,crop=216:384,"
    f"gblur=sigma=6,scale={CW}:{CH}:flags=bilinear[bgb];"
    f"[fg]scale={CW}:-2:flags=lanczos[fgs];"
    "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,ass={ass}:fontsdir=fonts[v]"
)


def render(clip, ass_name, out_name, cwd, crf=28):
    """縦型に焼く。ass/fontsdir/出力は全部 cwd 相対（Windowsのドライブレターで
    filtergraph のパーサが壊れるのを避ける）。"""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", Path(clip).name,
        "-filter_complex", VCHAIN.format(ass=ass_name),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        # 444pだと多くのプレイヤーで開けない。下書きでも420p固定を崩さない
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out_name,
    ]
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out_shorts")
    ap.add_argument("--fonts", default="fonts", help="BIZUDPGothic-Regular.ttf のあるディレクトリ")
    ap.add_argument("--crf", type=int, default=28)
    a = ap.parse_args()

    outdir = Path(a.out)
    mpath = outdir / "manifest.json"
    if not mpath.exists():
        sys.exit(f"error: {mpath} が無い (shorts_prep.py が先)")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))

    # ass の fontsdir も cwd 相対にするため、フォントを out 配下へ置く
    fsrc = Path(a.fonts)
    fdst = outdir / "fonts"
    fdst.mkdir(parents=True, exist_ok=True)
    for ttf in fsrc.glob("*.ttf"):
        shutil.copy2(ttf, fdst / ttf.name)
    if not list(fdst.glob("*.ttf")):
        sys.exit(f"error: フォントが無い ({fsrc}/*.ttf)")

    ok = failed = 0
    for c in manifest["clips"]:
        clip = outdir / c["file"]
        srt = outdir / c["srt"]
        short = f"short_{int(c['id']):02d}.mp4"
        ass_name = f"cap_{int(c['id']):02d}.ass"
        try:
            dur = probe_dur(clip)
            segs = parse_srt(srt) if srt.exists() else []
            title = (c.get("titles") or [""])[0]
            n = build_ass(segs, title, dur, outdir / ass_name)
            render(clip, ass_name, short, outdir, a.crf)
            size = (outdir / short).stat().st_size
            c["short"] = short
            c["short_bytes"] = size
            ok += 1
            print(f"clip {c['id']}: {short} {size/1048576:.1f}MB ({n} lines)", file=sys.stderr)
        except Exception as e:
            failed += 1
            c["short_error"] = str(e)[:200]
            print(f"clip {c['id']} RENDER FAILED: {e}", file=sys.stderr)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"rendered: {ok} ok, {failed} failed")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
