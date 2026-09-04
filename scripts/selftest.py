"""ネットワーク不要のセルフテスト (ストリップ方式)。

    python scripts/selftest.py
"""
import json
import re
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
    # レーン別速度: 既定で複数の速度が混在し、レーン再利用は時刻ベース(十分空けばレーン0へ戻る)
    check("layout: lane speeds vary", len(set(p.lane_speed)) >= 3 and min(p.lane_speed) > 0,
          f"({sorted(set(round(s) for s in p.lane_speed))})")
    check("layout: lane reused after gap", placed[3][1] == 0, f"(lane={placed[3][1]})")
    p1 = strip_render.Params(scale=2 / 3, cfg={"danmaku": {"speed_variants": [1.0]}})
    check("layout: speed_variants=[1.0] -> uniform", len(set(p1.lane_speed)) == 1)

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
        # ストリップごとの速度がmanifestに載り、幅は速度に比例する (burn側のoverlay式と整合)
        check("render: per-lane speed in manifest",
              all(abs(s["speed"] - p.lane_speed[s["lane"]]) < 1e-6 for s in m2["strips"]))
        widths = {}
        for s in m2["strips"]:
            with Image.open(d / "strips2" / s["files"][0]) as im_:
                widths[s["lane"]] = im_.width
        check("render: strip width follows lane speed",
              {0, 1, 2} <= set(widths) and widths[1] < widths[0] < widths[2], f"({widths})")


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
          and "かつき" in cs["title_template"])
    cs2 = channel_settings(cfg, "hashimotokun78")
    # 「君/くん」の表記ゆれは追わない。fallbackでなく本人のテンプレが来たかだけ見る
    check("yt: hashimoto settings", cs2["yt_token_env"] == "YT_TOKEN_JSON"
          and "はしもと" in cs2["title_template"], cs2["title_template"])
    cs3 = channel_settings(cfg, "unknown_channel")
    check("yt: unknown falls back", cs3["yt_token_env"] == "YT_TOKEN_JSON")
    cs4 = channel_settings(cfg, "zingisukan2525")
    check("yt: zingisukan settings", cs4["yt_token_env"] == "YT_TOKEN_JSON_WAINAINA"
          and "ジンギスカン" in cs4["title_template"])
    # 12h超の分割投稿: タイトルは（i/N）接尾辞・95字上限を接尾辞込みで守る
    from yt_upload import part_title
    check("yt: part title suffix", part_title("タイトル", 1, 2) == "タイトル（1/2）"
          and part_title("タイトル", 2, 2).endswith("（2/2）"))
    long_t = part_title("あ" * 120, 1, 2)
    check("yt: part title capped", len(long_t) == 95 and long_t.endswith("（1/2）"),
          f"(len={len(long_t)})")


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
        # タイトル内の絵文字がカラーで描けること (豆腐回帰テスト)
        emoji_font = find_emoji_font()
        if emoji_font:
            shaper, fit = thumbnail.fit_title("ニートの夏休み\U0001F349",
                                              font_path, emoji_font, 1200)
            check("thumb: emoji run kept",
                  any(k == "j" for k, _ in shaper.split_runs(fit)))
            em = thumbnail.render_emoji_opaque(shaper, "\U0001F349")
            colorful = False
            if em:
                for x in range(0, em.width, 3):
                    for y in range(0, em.height, 3):
                        r, g, b, aa = em.getpixel((x, y))
                        if aa > 200 and max(r, g, b) - min(r, g, b) > 60:
                            colorful = True
            check("thumb: emoji rendered in color", colorful)
            out2 = d / "t2.jpg"
            thumbnail.compose(str(frame), "ニートの夏休み\U0001F349", "2026/07/22",
                              font_path, str(out2), emoji_font_path=emoji_font)
            check("thumb: emoji compose ok",
                  out2.exists() and out2.stat().st_size > 10000)
        else:
            check("thumb: emoji font present", False, "(NotoColorEmoji.ttf missing)")


def test_chapters_logic():
    import chapters
    check("ch: hms zero-padded", chapters.hms(3725) == "01:02:05"
          and chapters.hms(65) == "01:05" and chapters.hms(0) == "00:00")
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
    # Geminiが改行せず1行に連結して返すケース (実際に起きた) を分解できること
    oneline = "0:00 配信開始 \n0:02:18 メイン会場へ徒歩移動 0:11:15 他配信者との合流と挨拶 0:42:44 バッジの受け取り"
    ch3 = chapters.validate_chapters(oneline, 20000)
    check("ch: one-line raw split", [c[1] for c in ch3] ==
          ["配信開始", "メイン会場へ徒歩移動", "他配信者との合流と挨拶", "バッジの受け取り"],
          f"({ch3})")
    b = chapters.bucketize([(0, "あ"), (10, "い"), (65, "う"), (66, "え" * 200)],
                           bucket=60, max_chars=50)
    check("ch: bucketize merge+cap", b[0] == (0, "あ い") and b[1][0] == 60
          and len(b[1][1]) == 50, f"({b})")
    # 並列文字起こし: 区間分割が1波(<=12)に収まり、各区間が動画末尾まで連続すること
    import plan as _plan
    segs = _plan.plan_segments(31858, 2700)  # 8.85h VOD
    check("ch: transcribe segs one-wave", len(segs) <= 12
          and segs[0]["start"] == 0 and segs[-1]["end"] == 31858
          and all(segs[i]["end"] == segs[i + 1]["start"] for i in range(len(segs) - 1)),
          f"(n={len(segs)})")
    # finalize の結合: 区間ファイルを絶対時刻でソート統合 (順不同・区間内も順不同を許容)
    merged = []
    for s in [{"lines": [[2705.0, "後半A"], [2701.0, "後半B"]]},
              {"lines": [[10.0, "前半A"], [5.0, "前半B"]]}]:
        merged.extend(s["lines"])
    merged.sort(key=lambda x: x[0])
    check("ch: finalize merge+sort", [t for t, _ in merged] == [5.0, 10.0, 2701.0, 2705.0],
          f"({merged})")
    # 分割投稿のパート切り出し: 境界の行はパート2側・パート内時刻(0起点)へ変換
    pl = chapters.slice_part_lines(
        [(10.0, "前半"), (23570.0, "境界"), (23580.0, "後半"), (47000.0, "末尾")],
        23570.0, 23571.0)
    check("ch: part slice+shift", pl == [(0.0, "境界"), (10.0, "後半"), (23430.0, "末尾")],
          f"({pl})")
    check("ch: part slice excludes before",
          chapters.slice_part_lines([(10.0, "前半")], 23570.0, 23571.0) == [])
    # 文字起こし保管: ヘッダ(title/url)付き・各行 [HH:MM:SS] 本文・時刻から復元可能
    tx = chapters.format_transcript(
        {"title": "配信タイトル", "yt_url": "https://x", "slug": "hashimotokun78",
         "uuid": "u1"}, [[0.0, "はじまり"], [3725.0, "ラーメン"]])
    check("ch: transcript format", tx.startswith("# 配信タイトル\nhttps://x")
          and "[00:00:00] はじまり" in tx and "[01:02:05] ラーメン" in tx
          and chapters.parse_ts("01:02:05") == 3725, f"({tx!r})")
    desc = "はしもと君のKICK配信の録画アーカイブです。\n\nタイムスタンプ▽\n\n\n元配信▽\nタイトル\nhttps://x\n\n#タグ"
    new = chapters.inject_description(desc, [(0, "配信開始"), (300, "移動")])
    check("ch: inject keeps head/tail",
          new.startswith("はしもと君の") and "00:00 配信開始\n05:00 移動" in new
          and "元配信▽\nタイトル" in new and new.rstrip().endswith("#タグ"))
    # 再実行しても二重にならない
    new2 = chapters.inject_description(new, [(0, "配信開始"), (600, "別の章")])
    check("ch: inject idempotent", "05:00 移動" not in new2 and "10:00 別の章" in new2)
    # retrofit: 壊れた1行連結ブロックの再整形 + ににまる→かつき改名
    import retrofit_format
    broken = ("ににまるのKICK配信の録画アーカイブです。\n\nタイムスタンプ▽\n"
              "0:00 配信開始 \n0:02:18 徒歩移動 0:11:15 合流と挨拶 1:21:30 ブース紹介"
              "\n\n元配信▽\nタイトル\nhttps://x\n\n#キッカーズ #ににまる")
    fixed = retrofit_format.reformat_description(broken, "220ninimaru")
    check("rf: block reformatted",
          "00:00 配信開始\n02:18 徒歩移動\n11:15 合流と挨拶\n01:21:30 ブース紹介" in fixed,
          f"({fixed!r})")
    check("rf: rename ににまる→かつき", fixed.startswith("かつきのKICK配信")
          and "#キッカーズ #かつき #ににまる" in fixed)
    check("rf: reformat idempotent",
          retrofit_format.reformat_description(fixed, "220ninimaru") == fixed)
    check("rf: retitle", retrofit_format.retitle("【ににまる】昼配信【2026/08/05】",
          "220ninimaru") == "【かつき】昼配信【2026/08/05】"
          and retrofit_format.retitle("【はしもと君】朝【2026/08/05】", "hashimotokun78")
          == "【はしもとくん】朝【2026/08/05】"
          and retrofit_format.retitle("【はしもとくん】朝【2026/08/05】", "hashimotokun78")
          == "【はしもとくん】朝【2026/08/05】")


def test_watch_stale_logic():
    from datetime import datetime, timedelta, timezone
    import watch
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(hours=1)
    mid = now - timedelta(hours=3)
    old = now - timedelta(hours=13)
    check("stale: fresh never", not watch.dispatched_is_stale(fresh, now, False))
    check("stale: 3h + idle -> stale", watch.dispatched_is_stale(mid, now, False))
    check("stale: 3h + busy -> not", not watch.dispatched_is_stale(mid, now, True))
    check("stale: 3h + unknown -> not", not watch.dispatched_is_stale(mid, now, None))
    check("stale: 13h always", watch.dispatched_is_stale(old, now, None))

    # チャンネル優先度: config.channelsの並び順 (はしもと君 > かつき)。新旧より優先
    def vod(slug, uuid, days_ago):
        start = now - timedelta(days=days_ago)
        return {"video": {"uuid": uuid}, "is_live": False,
                "duration": 3600_000, "session_title": uuid,
                "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                "_slug": slug}
    cfg = {"channels": ["hashimotokun78", "220ninimaru"], "backlog": True,
           "daily_upload_limit": 6, "max_inflight": 3, "dispatch_batch": 3}
    videos = [vod("220ninimaru", "n-old", 10), vod("hashimotokun78", "h-new", 1),
              vod("220ninimaru", "n-mid", 5), vod("hashimotokun78", "h-old", 8)]
    picks, exhausted = watch.pick_dispatches(videos, {}, cfg, now, active_runs=False)
    check("watch: hashimoto first then oldest",
          [p["uuid"] for p in picks] == ["h-old", "h-new", "n-old"],
          f"({[p['uuid'] for p in picks]})")
    check("watch: no exhausted on fresh", exhausted == [])

    # ---- 自己修復: 再試行上限(累計5回=retries4)と needs-review ----
    stale = (now - timedelta(hours=13)).isoformat()
    def st(status, retries=None):
        d = {"status": status, "dispatched_at": stale}
        if retries is not None:
            d["retries"] = retries
        return d
    videos2 = [vod("hashimotokun78", f"u{i}", 5 + i) for i in range(6)]
    states = {
        "u0": st("dispatched", 2),          # 5回未満 → 再投入対象
        "u1": st("dispatched", 4),          # 5回目も失敗(=retries4でstale) → 停止対象
        "u2": st("needs-review", 4),        # 停止済み → 二度と投入しない
        "u3": st("skipped-subonly"),        # 別分類(業務スキップ) → 影響なし
        "u4": st("dispatched"),             # 旧台帳互換(retriesキー無し=0扱い) → 再投入対象
        "u5": {"status": "done"},           # 完了 → 影響なし
    }
    picks2, ex2 = watch.pick_dispatches(videos2, states, cfg, now, active_runs=False)
    pids = {p["uuid"] for p in picks2}
    check("watch: under-cap retried", "u0" in pids and "u4" in pids, f"({pids})")
    check("watch: cap reached -> exhausted only",
          [e["uuid"] for e in ex2] == ["u1"], f"({ex2})")
    check("watch: needs-review never re-dispatched", "u2" not in pids)
    check("watch: other classes untouched", "u3" not in pids and "u5" not in pids)
    # 枠ゼロでも上限超過の記録は返る
    cfg0 = dict(cfg, daily_upload_limit=0)
    picks3, ex3 = watch.pick_dispatches(videos2, states, cfg0, now, active_runs=False)
    check("watch: exhausted reported even with no allowance",
          picks3 == [] and [e["uuid"] for e in ex3] == ["u1"])


def test_mark_done_import():
    import mark_done  # noqa: F401  (curl_cffi非依存であること)
    check("mark_done: importable without curl_cffi deps", True)


def test_pick_height():
    """ランナー残ディスクからの画質フォールバック判定 (burn.pick_height)。"""
    from burn import pick_height

    GB = 1e9
    # 7h VOD, seg 90分, 40GB空き -> 1080のまま (need約28.6GB)
    h = pick_height(1080, 720, 40 * GB, 25200, 5400)
    check("ph: 7h/40GB -> 1080", h == 1080, f"({h})")
    # 14.8h VOD, 40GB空き -> 720へ降格 (need約56GB)
    h = pick_height(1080, 720, 40 * GB, 53280, 5400)
    check("ph: 14.8h/40GB -> 720", h == 720, f"({h})")
    # durationが0/不明ならチェックをスキップしてwantのまま
    h = pick_height(1080, 720, 1 * GB, 0, 5400)
    check("ph: duration=0 -> want (skip check)", h == 1080, f"({h})")
    h = pick_height(1080, 720, 1 * GB, None, 5400)
    check("ph: duration=None -> want (skip check)", h == 1080, f"({h})")
    # burnonly指定でwant=720のときはfallbackも720なので降格しようがなくwantのまま
    h = pick_height(720, 720, 5 * GB, 53280, 5400)
    check("ph: want=720/5GB/14.8h -> 720 (fallback not lower)", h == 720, f"({h})")


def test_burn_request():
    import burn_request
    m = burn_request.KICK_URL_RE.search(
        "https://kick.com/hashimotokun78/videos/019fbc35-23b8-7aaa-b384-2b250e8e75bc")
    check("br: kick url parse", m and m.group(1) == "hashimotokun78"
          and m.group(2).startswith("019fbc35"))
    m2 = burn_request.TWITCH_URL_RE.search("https://www.twitch.tv/videos/2770109916?t=1s")
    check("br: twitch url parse", m2 and m2.group(1) == "2770109916")
    check("br: safe_name", burn_request.safe_name('あ/い:う*え テスト') == "あ_い_う_え_テスト"
          and burn_request.safe_name("") == "video")
    import twitch_chat_fetch  # noqa: F401  (import可能なこと)
    check("br: twitch_chat_fetch importable", True)


def test_shorts_render():
    """縦型下書きの字幕が画面幅に収まること。

    jp_wrap は max_lines を超えると per_line を破って詰め込むため、
    16文字折り返しのままだと実際に左右が切れて焼き上がった(実測)。
    行数側を緩めて文字数側を必ず守らせる形に直した回帰テスト。
    """
    import shorts_render as sr

    NL = chr(10)
    srt = NL.join(["1", "00:00:00,500 --> 00:00:03,200", "いやこれめっちゃうまいんだけど", "",
                   "2", "00:00:03,400 --> 00:00:07,100", "なんか時代終わったね あの子は"])
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "a.srt"
        sp.write_text(srt, encoding="utf-8")
        segs = sr.parse_srt(sp)
    check("sr: srt 2件パース", len(segs) == 2, f"{len(segs)}件")
    check("sr: srt 時刻", bool(segs) and abs(segs[0]["start"] - 0.5) < 1e-6
          and abs(segs[-1]["end"] - 7.1) < 1e-6)
    check("sr: srt 本文", bool(segs) and segs[-1]["text"] == "なんか時代終わったね あの子は")

    long = "もう一回やるからちゃんと見ててねこれ本当に大事なところだから絶対に見逃さないで"
    cases = [long, "なんか時代終わったね あの子は", "あ" * 51, "www", "え？"]
    segs = [{"start": i * 2, "end": i * 2 + 1.5, "text": t} for i, t in enumerate(cases)]
    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / "cap.ass"
        sr.build_ass(segs, "まさかの展開だった", 60.0, ap)
        body = ap.read_text(encoding="utf-8")
    over = []
    n_dialogue = 0
    for ln in body.splitlines():
        if not ln.startswith("Dialogue:"):
            continue
        n_dialogue += 1
        parts = ln.split(",", 9)
        style, text = parts[3], parts[-1]
        text = re.sub("[{][^}]*[}]", "", text)      # ASS override タグを除去
        limit = sr.TITLE_WRAP if style == "Title" else sr.CAP_WRAP
        for row in text.split(chr(92) + "N"):
            if len(row) > limit:
                over.append(f"{style}:{len(row)}>{limit}:{row[:14]}")
    check("sr: 全行が折り返し上限内(はみ出し回帰)", not over, f"超過{len(over)}件 {over[:2]}")
    check("sr: タイトル+字幕が出ている", n_dialogue == len(cases) + 1, f"{n_dialogue}行")
    check("sr: 420p固定を明示している",
          "yuv420p" in (Path(__file__).with_name("shorts_render.py")
                        .read_text(encoding="utf-8")))

def test_shorts_pick():
    """候補選定(候補ボードからの移植)が効いていること。

    実データ照合は verify 済み(8/19配信・チャット33,546行で20件中20件の
    score/rel/msgs が一致)。ここは CI で回せる合成データの範囲を見る。
    """
    import shorts_pick as sp

    rows = [(float(t), "ふつうのコメント") for t in range(0, 1200, 10)]   # 平常帯
    rows += [(600.0 + i * 0.5, "www") for i in range(60)]                 # 600-630 に爆発
    rows.sort()
    cands = sp.find_candidates(rows, topn=5)
    check("sp: ピークを拾う", bool(cands), f"{len(cands)}件")
    if cands:
        top = cands[0]
        check("sp: ピーク位置が合っている", 560 <= top["start"] <= 620, f"start={top['start']}")
        check("sp: 笑いタグが付く", "🤣爆笑" in top["tags"], f"{top['tags']}")
        check("sp: 平常比が1超", top["rel"] > 1.0, f"rel={top['rel']}")

    # 長い候補はショート尺に詰める(実測で200秒級が混ざる)
    s, e = sp.tighten(rows, 500.0, 700.0, 70.0, 30.0)
    check("sp: 長い候補をmax_secへ", abs((e - s) - 70.0) < 0.11, f"{e - s}s")
    check("sp: 詰め先がピーク側", 560 <= s <= 620, f"start={s}")
    s2, e2 = sp.tighten(rows, 100.0, 110.0, 70.0, 30.0)
    check("sp: 短い候補をmin_secへ", abs((e2 - s2) - 30.0) < 0.11, f"{e2 - s2}s")
    s3, e3 = sp.tighten(rows, 2.0, 5.0, 70.0, 30.0)
    check("sp: 先頭で負にならない", s3 >= 0.0, f"start={s3}")

    dup = [{"start": 600, "end": 660, "score": 9, "rel": 3.0, "msgs": 9, "tags": []},
           {"start": 605, "end": 665, "score": 8, "rel": 2.0, "msgs": 8, "tags": []},
           {"start": 100, "end": 160, "score": 7, "rel": 1.5, "msgs": 7, "tags": []}]
    segs = sp.to_segments(rows, dup, 8, 70.0, 30.0)
    check("sp: かぶった候補を落とす", len(segs) == 2, f"{len(segs)}本")
    check("sp: idが連番", [x["id"] for x in segs] == list(range(1, len(segs) + 1)))
    check("sp: 本数上限を守る", len(sp.to_segments(rows, dup, 1, 70.0, 30.0)) == 1)
    check("sp: 空チャットで落ちない", sp.find_candidates([]) == [])


def test_shorts_dispatch():
    """ショート下書きの連鎖は shorts_channels のチャンネルだけに出ること。

    アーカイブ本線(process)は3ch全部を回すので、ここが漏れると他人のチャンネルの
    ショートまで作り始める。
    """
    import watch

    picks = [{"slug": "zingisukan2525", "uuid": "a", "title": "t"},
             {"slug": "hashimotokun78", "uuid": "b", "title": "t"}]
    got = watch.shorts_targets(picks, {"shorts_channels": ["zingisukan2525"]})
    check("watch: shorts対象を絞る", [c["uuid"] for c in got] == ["a"], f"{[c['uuid'] for c in got]}")
    check("watch: 未設定なら何もしない", watch.shorts_targets(picks, {}) == [])
    check("watch: 空でも落ちない", watch.shorts_targets([], {"shorts_channels": ["x"]}) == [])

    cfgp = Path(__file__).parent.parent / "config.json"
    cfg = json.loads(cfgp.read_text(encoding="utf-8"))
    check("watch: configにshorts_channelsがある", bool(cfg.get("shorts_channels")),
          str(cfg.get("shorts_channels")))
    check("watch: shorts対象はchannelsの部分集合",
          set(cfg.get("shorts_channels") or []) <= set(cfg.get("channels") or []),
          "監視していないチャンネルは連鎖できない")


def test_shorts_title_clean():
    """Geminiのタイトル案からラベルを落とすこと。

    出力形式に「案1 / 案2」と書いたら、そのまま "案1 犯人はあのメガネの人!?" が
    返ってきて縦型のヘッダーに焼き込まれた(実測・run 32741421262)。プロンプトを
    直しただけでは再発しうるので、受け側でも必ず落とす。
    """
    import shorts_prep as sp

    cases = [("案1 犯人はあのメガネの人!?", "犯人はあのメガネの人!?"),
             ("案2 写真を撮った犯人とは", "写真を撮った犯人とは"),
             ("案 3 前後に空白 ", "前後に空白"),
             ("1. まさかの展開", "まさかの展開"),
             ("・温泉で事故", "温泉で事故"),
             ("「引用付き」", "引用付き"),
             ("ふつうのタイトル", "ふつうのタイトル"),
             ("", "")]
    bad = [(i, want, sp.clean_title(i)) for i, want in cases if sp.clean_title(i) != want]
    check("sp: タイトルのラベル除去", not bad, str(bad[:2]))
    check("sp: 出力形式にラベル例を残していない", "案1\n案2" not in sp.TITLE_PROMPT)


def main():
    test_plan_segments()
    test_tokenize()
    test_layout_and_render()
    test_templates()
    test_yt_title_sanitize()
    test_thumbnail()
    test_chapters_logic()
    test_watch_stale_logic()
    test_mark_done_import()
    test_burn_request()
    test_pick_height()
    test_shorts_render()
    test_shorts_pick()
    test_shorts_dispatch()
    test_shorts_title_clean()
    if FAILED:
        print(f"\n{len(FAILED)} FAILED: {FAILED}")
        sys.exit(1)
    print("\nALL OK")


if __name__ == "__main__":
    main()
