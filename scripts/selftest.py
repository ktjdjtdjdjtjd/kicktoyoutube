"""ネットワーク不要のセルフテスト (ストリップ方式)。

    python scripts/selftest.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import emotes as emotes_mod
import strip_render
from plan import plan_segments

FAILED = []


def check(name, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        FAILED.append(name)


def find_font():
    candidates = [
        Path(__file__).parent.parent / "fonts" / "BIZUDPGothic-Regular.ttf",
        Path(r"C:\Windows\Fonts\BIZ-UDPGothicR.ttc"),
        Path(r"C:\Windows\Fonts\msgothic.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def test_plan_segments():
    s = plan_segments(3000, 5400)
    check("plan: short vod -> 1 seg", len(s) == 1 and s[0]["end"] == 3000)
    s = plan_segments(8118, 5400)
    check("plan: 8118s -> 2 segs", len(s) == 2 and s[1] == {"idx": 1, "start": 5400, "end": 8118})
    s = plan_segments(5400 + 600, 5400)
    check("plan: 10min tail merged", len(s) == 1 and s[0]["end"] == 6000)
    s = plan_segments(29051, 5400)
    check("plan: 8h contiguous", all(
        s[i]["end"] == s[i + 1]["start"] for i in range(len(s) - 1)) and s[-1]["end"] == 29051)


def test_tokenize():
    t = emotes_mod.tokenize("あはは[emote:123:Sadge]ｗ[emote:45:Pog]")
    check("tokenize: mixed", t == [("text", "あはは"), ("emote", "123"),
                                   ("text", "ｗ"), ("emote", "45")])
    t = emotes_mod.tokenize("plain text")
    check("tokenize: plain", t == [("text", "plain text")])
    t = emotes_mod.tokenize("[emote:9:x]")
    check("tokenize: emote only", t == [("emote", "9")])
    ids = emotes_mod.collect_ids([(0, "[emote:1:a] [emote:2:b]"), (1, "[emote:1:a]")])
    check("collect_ids: dedup", ids == {"1", "2"})


def find_emoji_font():
    p = Path(__file__).parent.parent / "fonts" / "NotoColorEmoji.ttf"
    return str(p) if p.exists() else None


def test_layout_and_render():
    font_path = find_font()
    if not font_path:
        check("layout: font available", False, "(no font found)")
        return
    from PIL import Image
    p = strip_render.Params(scale=2 / 3)  # 720p相当
    shaper = strip_render.TextShaper(font_path, find_emoji_font(), p)

    msgs = [(0.0, "こんにちは"), (0.5, "second"), (1.0, "third"),
            (100.0, "[emote:777:Test]ｗｗｗ[emote:888:Anim]"), (200.0, "あ" * 200)]
    placed = strip_render.layout(msgs, shaper, p)
    check("layout: all placed", len(placed) == 5)
    lanes3 = {pl[1] for pl in placed[:3]}
    check("layout: no lane collision", len(lanes3) == 3, f"(lanes={lanes3})")
    check("layout: long msg capped", placed[4][3] <= p.max_msg_w + 1)

    # 絵文字run分割
    runs = shaper.split_runs("草\U0001F602www")
    if shaper.emoji_font:
        check("shaper: emoji split", ("j", "\U0001F602") in runs, f"({runs})")
    else:
        check("shaper: emoji font present", False, "(NotoColorEmoji.ttf missing)")
    runs2 = shaper.split_runs("普通のテキスト")
    check("shaper: plain stays text", runs2 == [("t", "普通のテキスト")])

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "emotes").mkdir()
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(d / "emotes" / "777.png")
        # 2フレームGIF (赤/青)
        f1 = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
        f2 = Image.new("RGBA", (64, 64), (0, 0, 255, 255))
        f1.save(d / "emotes" / "888.gif", save_all=True, append_images=[f2],
                duration=200, loop=0)
        chat = d / "chat.jsonl"
        chat.write_text("\n".join(json.dumps({"rel": r, "content": c}, ensure_ascii=False)
                                  for r, c in msgs), encoding="utf-8")
        m = strip_render.build_for_segment(chat, 90.0, 210.0, font_path,
                                           d / "emotes", d / "strips", scale=2 / 3,
                                           emoji_font_path=find_emoji_font())
        check("render: manifest strips", len(m["strips"]) >= 1)
        # GIF入りレーンは位相4変種
        anim_lane = next((s for s in m["strips"] if len(s["files"]) > 1), None)
        check("render: animated lane has phases", anim_lane is not None
              and len(anim_lane["files"]) == p.gif_phases)
        if anim_lane:
            # 位相間で画素が変わる (赤フレームと青フレーム)
            with Image.open(d / "strips" / anim_lane["files"][0]) as a_:
                im_a = a_.convert("RGBA")
            with Image.open(d / "strips" / anim_lane["files"][1]) as b_:
                im_b = b_.convert("RGBA")
            check("render: phases differ", im_a.tobytes() != im_b.tobytes())
        with Image.open(d / "strips" / m["strips"][0]["files"][0]) as s0:
            h0 = s0.height
        check("render: strip height", h0 == p.lane_h, f"(h={h0})")
        m2 = strip_render.build_for_segment(chat, 0.0, 120.0, font_path,
                                            d / "emotes", d / "strips2", scale=2 / 3,
                                            emoji_font_path=find_emoji_font())
        check("render: cross-segment lanes stable",
              {s["lane"] for s in m2["strips"]} >= {pl[1] for pl in placed[:3]})


def test_templates():
    cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
    fields = {"date": "2026-08-01", "date_slash": "2026/08/01", "title": "テスト配信",
              "channel": "hashimotokun78", "url": "https://kick.com/x"}
    t = cfg["title_template"].format(**fields)
    d = cfg["description_template"].format(**fields)
    check("config: title format", t == "【はしもと君】テスト配信【2026/08/01】", f"({t})")
    check("config: desc has header", d.startswith("はしもと君のKICK配信の録画アーカイブです。"))
    check("config: desc timestamps section", "タイムスタンプ▽" in d)
    check("config: desc source section", "元配信▽\nテスト配信\nhttps://kick.com/x" in d)
    check("config: desc hashtags", d.rstrip().endswith("#キッカーズ #はしもと君 #横山緑"))
    check("config: privacy valid", cfg["privacy"] in ("public", "unlisted", "private"))


def test_yt_title_sanitize():
    from yt_upload import sanitize_title, channel_settings
    check("yt: angle brackets replaced", sanitize_title("a<b>c") == "a＜b＞c")
    check("yt: 100char truncated", len(sanitize_title("あ" * 200)) == 95)
    cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
    cs = channel_settings(cfg, "220ninimaru")
    check("yt: ninimaru settings", cs["yt_token_env"] == "YT_TOKEN_JSON_NINIMARU"
          and "ににまる" in cs["title_template"])
    cs2 = channel_settings(cfg, "hashimotokun78")
    check("yt: hashimoto settings", cs2["yt_token_env"] == "YT_TOKEN_JSON"
          and "はしもと君" in cs2["title_template"])
    cs3 = channel_settings(cfg, "unknown_channel")
    check("yt: unknown falls back", cs3["yt_token_env"] == "YT_TOKEN_JSON")


def test_thumbnail():
    font_path = find_font()
    if not font_path:
        check("thumb: font available", False)
        return
    import thumbnail
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        chat = d / "chat.jsonl"
        rows = [{"rel": 400 + i * 0.5, "content": "w"} for i in range(100)]
        rows += [{"rel": 1000 + i * 10, "content": "x"} for i in range(5)]
        chat.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        peak = thumbnail.find_hype_peak(chat, 3600)
        check("thumb: peak in dense window", 390 <= peak <= 480, f"(peak={peak})")
        frame = d / "f.png"
        Image.new("RGB", (1280, 720), (30, 60, 90)).save(frame)
        out = d / "t.jpg"
        thumbnail.compose(str(frame), "テストタイトル" * 6, "2026/08/01", font_path, str(out))
        check("thumb: output exists", out.exists() and out.stat().st_size > 10000)
        with Image.open(out) as im:
            check("thumb: 1280x720", im.size == (1280, 720))
            px = im.convert("RGB").load()
            # 座布団のパディング部 (文字に当たらない右下端寄り) を見る
            corner = px[1280 - 34, 720 - 34]
            check("thumb: date plate bottom-right", sum(corner) < 200, f"({corner})")


def test_chapters_logic():
    import chapters
    check("ch: hms", chapters.hms(3725) == "1:02:05" and chapters.hms(65) == "1:05")
    check("ch: parse_ts", chapters.parse_ts("1:02:05") == 3725 and chapters.parse_ts("0:00") == 0)
    raw = """はい、チャプターです。
0:00 配信開始
0:05:00 コンビニへ移動
0:05:30 近すぎる章
1:20:00 ラーメン実食
9:99 壊れた行
2:00:00 これは尺の外
"""
    ch = chapters.validate_chapters(raw, 6000)
    check("ch: validated", [c[1] for c in ch] == ["配信開始", "コンビニへ移動", "ラーメン実食"],
          f"({ch})")
    ch2 = chapters.validate_chapters("2:00 いきなり途中から", 6000)
    check("ch: 0:00 auto-prepended", ch2[0] == (0, "配信開始"))
    b = chapters.bucketize([(0, "あ"), (10, "い"), (65, "う"), (66, "え" * 200)],
                           bucket=60, max_chars=50)
    check("ch: bucketize merge+cap", b[0] == (0, "あ い") and b[1][0] == 60
          and len(b[1][1]) == 50, f"({b})")
    desc = "はしもと君のKICK配信の録画アーカイブです。\n\nタイムスタンプ▽\n\n\n元配信▽\nタイトル\nhttps://x\n\n#タグ"
    new = chapters.inject_description(desc, [(0, "配信開始"), (300, "移動")])
    check("ch: inject keeps head/tail",
          new.startswith("はしもと君の") and "0:00 配信開始\n5:00 移動" in new
          and "元配信▽\nタイトル" in new and new.rstrip().endswith("#タグ"))
    # 再実行しても二重にならない
    new2 = chapters.inject_description(new, [(0, "配信開始"), (600, "別の章")])
    check("ch: inject idempotent", "5:00 移動" not in new2 and "10:00 別の章" in new2)


def test_mark_done_import():
    import mark_done  # noqa: F401  (curl_cffi非依存であること)
    check("mark_done: importable without curl_cffi deps", True)


def main():
    test_plan_segments()
    test_tokenize()
    test_layout_and_render()
    test_templates()
    test_yt_title_sanitize()
    test_thumbnail()
    test_chapters_logic()
    test_mark_done_import()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {FAILED}")
        sys.exit(1)
    print("\nALL OK")


if __name__ == "__main__":
    main()
