"""ネットワーク不要のセルフテスト。ロジック回帰用 (ASS生成 / スライス / セグメント計画 / テンプレート)。

    python scripts/selftest.py
"""
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chat_to_ass
import slice_ass as slicer
from plan import plan_segments

FAILED = []


def check(name, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        FAILED.append(name)


def test_plan_segments():
    s = plan_segments(3000, 5400)
    check("plan: short vod -> 1 seg", len(s) == 1 and s[0]["end"] == 3000)
    s = plan_segments(8118, 5400)
    # 端数 2718s(45min) >= 20min なので2分割のまま
    check("plan: 8118s -> 2 segs", len(s) == 2 and s[1] == {"idx": 1, "start": 5400, "end": 8118})
    s = plan_segments(5400 + 600, 5400)
    # 端数10分 < 20分 -> 併合
    check("plan: 10min tail merged", len(s) == 1 and s[0]["end"] == 6000)
    s = plan_segments(21846, 5400)
    check("plan: 6h -> contiguous", all(
        s[i]["end"] == s[i + 1]["start"] for i in range(len(s) - 1)) and s[-1]["end"] == 21846)


def test_ass_roundtrip():
    msgs = [(1.0, "こんにちは"), (2.0, "abc {test}"), (3.5, "ｗｗｗ" * 30)]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "t.ass"
        chat_to_ass.write_ass(msgs, str(out))
        text = out.read_text(encoding="utf-8-sig")
    check("ass: BIZ UDPGothic style", "Style: Danmaku,BIZ UDPGothic,65" in text)
    check("ass: white color", "\\c&HFFFFFF&" in text)
    check("ass: 3 events", text.count("Dialogue:") == 3)
    check("ass: braces sanitized", "{test}" not in text and "(test)" in text)
    check("ass: move tag parseable", all(
        slicer.DIALOGUE_RE.match(l) for l in text.splitlines() if l.startswith("Dialogue:")))


def test_slice():
    msgs = [(10.0, "before boundary"), (98.0, "crosses boundary"), (150.0, "inside"), (300.0, "after")]
    with tempfile.TemporaryDirectory() as d:
        full = Path(d) / "full.ass"
        chat_to_ass.write_ass(msgs, str(full))
        lines = full.read_text(encoding="utf-8-sig").splitlines()
    out, n = slicer.slice_ass(lines, 100.0, 200.0)
    check("slice: 2 events kept", n == 2, f"(got {n})")
    dial = [l for l in out if l.startswith("Dialogue:")]
    m = slicer.DIALOGUE_RE.match(dial[0])
    layer, t0, t1, pre, x1, y1, x2, y2, post = m.groups()
    check("slice: boundary event starts at 0", slicer.parse_time(t0) == 0.0)
    # 98s開始・10s表示のイベントを100sで切ると 2/10 進んだ位置から始まるはず
    orig_x1, orig_x2 = 1920.0, None
    m2 = re.search(r"\\move\(1920,", "\n".join(lines))
    check("slice: original starts at screen edge", m2 is not None)
    x1f = float(x1)
    check("slice: boundary x interpolated", 0 < x1f < 1920, f"(x1={x1f})")
    check("slice: end shifted", abs(slicer.parse_time(t1) - 8.0) < 0.05)
    m3 = slicer.DIALOGUE_RE.match(dial[1])
    check("slice: inside event shifted to 50s", slicer.parse_time(m3.group(2)) == 50.0)
    # ヘッダが保持されること
    check("slice: header kept", any("[V4+ Styles]" in l for l in out))


def test_templates():
    cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
    fields = {"date": "2026-08-01", "title": "テスト配信", "channel": "yoshihisa", "url": "https://kick.com/x"}
    t = cfg["title_template"].format(**fields)
    d = cfg["description_template"].format(**fields)
    check("config: title template", "2026-08-01" in t and "テスト配信" in t)
    check("config: description template", "https://kick.com/x" in d)
    check("config: privacy valid", cfg["privacy"] in ("public", "unlisted", "private"))
    check("config: channels non-empty", len(cfg["channels"]) > 0)


def test_yt_title_sanitize():
    sys.path.insert(0, str(Path(__file__).parent))
    from yt_upload import sanitize_title
    check("yt: angle brackets replaced", sanitize_title("a<b>c") == "a＜b＞c")
    check("yt: 100char truncated", len(sanitize_title("あ" * 200)) == 95)


def main():
    test_plan_segments()
    test_ass_roundtrip()
    test_slice()
    test_templates()
    test_yt_title_sanitize()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {FAILED}")
        sys.exit(1)
    print("\nALL OK")


if __name__ == "__main__":
    main()
