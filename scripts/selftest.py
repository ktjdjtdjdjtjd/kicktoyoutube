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


def test_layout_and_render():
    font_path = find_font()
    if not font_path:
        check("layout: font available", False, "(no font found)")
        return
    from PIL import Image, ImageFont
    p = strip_render.Params(scale=2 / 3)  # 720p相当
    font = ImageFont.truetype(font_path, p.font_px)

    msgs = [(0.0, "こんにちは"), (0.5, "second"), (1.0, "third"),
            (100.0, "[emote:777:Test]ｗｗｗ"), (200.0, "あ" * 200)]
    placed = strip_render.layout(msgs, font, p)
    check("layout: all placed", len(placed) == 5)
    # 同時刻帯の3件は別レーン
    lanes3 = {pl[1] for pl in placed[:3]}
    check("layout: no lane collision", len(lanes3) == 3, f"(lanes={lanes3})")
    # 長文は切り詰め
    check("layout: long msg capped", placed[4][3] <= p.max_msg_w + 1)

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # ダミーエモート
        (d / "emotes").mkdir()
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(d / "emotes" / "777.png")
        chat = d / "chat.jsonl"
        chat.write_text("\n".join(json.dumps({"rel": r, "content": c}, ensure_ascii=False)
                                  for r, c in msgs), encoding="utf-8")
        m = strip_render.build_for_segment(chat, 90.0, 210.0, font_path,
                                           d / "emotes", d / "strips", scale=2 / 3)
        check("render: manifest strips", len(m["strips"]) >= 1)
        check("render: manifest json", (d / "strips" / "strips.json").exists())
        with Image.open(d / "strips" / m["strips"][0]["file"]) as s0:
            h0 = s0.height
        check("render: strip height", h0 == p.lane_h, f"(h={h0})")
        # エモート(赤)が描かれているか: msg@100s -> x = LM + (100-90)*speed 付近
        found_red = False
        for st in m["strips"]:
            with Image.open(d / "strips" / st["file"]) as imf:
                im = imf.convert("RGBA")
            x0 = int(p.left_margin + 10 * p.speed)
            region = im.crop((x0, 0, min(x0 + 200, im.width), im.height))
            px = region.load()
            for yy in range(region.height):
                for xx in range(region.width):
                    r, g, b, aa = px[xx, yy]
                    if r > 200 and g < 60 and b < 60 and aa > 100:
                        found_red = True
                        break
                if found_red:
                    break
            if found_red:
                break
        check("render: emote pixels present", found_red)
        # セグメント境界の連続性: 同メッセージが別セグメントでも同レーン
        m2 = strip_render.build_for_segment(chat, 0.0, 120.0, font_path,
                                            d / "emotes", d / "strips2", scale=2 / 3)
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
    from yt_upload import sanitize_title
    check("yt: angle brackets replaced", sanitize_title("a<b>c") == "a＜b＞c")
    check("yt: 100char truncated", len(sanitize_title("あ" * 200)) == 95)


def test_mark_done_import():
    import mark_done  # noqa: F401  (curl_cffi非依存であること)
    check("mark_done: importable without curl_cffi deps", True)


def main():
    test_plan_segments()
    test_tokenize()
    test_layout_and_render()
    test_templates()
    test_yt_title_sanitize()
    test_mark_done_import()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {FAILED}")
        sys.exit(1)
    print("\nALL OK")


if __name__ == "__main__":
    main()
