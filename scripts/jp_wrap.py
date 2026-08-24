"""日本語字幕の改行位置を決める（意味の切れ目で折る）。

これは「スタイル」であって汎用ライブラリではない。この環境の字幕の癖
（空白＝文字起こしが与えた句切り、禁則、助詞の後ろで折る）をここに集約する。
combo_short と auto-shorts の両方が同じ関数を使う。片方だけ直すと必ずズレる。

旧実装は 13文字ごとに機械的にぶつ切りしていたため、
  「なんか時代終わったね あの子は」 -> 「なんか時代終わったね あの」+「子は」
のように語の途中で割れていた（実際に納品物に出ていた）。

  python jp_wrap.py    # 自己テスト
"""
from __future__ import annotations

# 行頭に来てはいけない文字（禁則）。小書き仮名・長音・閉じ括弧・句読点。
# 「ん」も入れる。単独で行を始めることは日本語ではあり得ない
# （実例: 「だって今たくさ / ん可愛い女の子いるから」）。
NO_START = ("、。，．,.・:：;；?？!！»”’"
            "」』）〉》］｝〕】"
            "ーー〜～"
            "ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ"
            "んン"
            "々ゝゞヽヾ")
# 行末に来てはいけない文字（開き括弧）。
NO_END = "「『（〈《［｛〔【“‘«"

# この直後は意味の切れ目になりやすい（助詞・接続）。
PARTICLES = ("は", "が", "を", "に", "へ", "で", "と", "も", "ね", "よ", "な",
             "さ", "か", "ら", "し", "て", "ど", "の")
PUNCT = "、。！？!?"

_SPACES = " 　\t"


def _is_hira(ch: str) -> bool:
    return "ぁ" <= ch <= "ゖ" or ch in "ゝゞー"


def _break_penalty(s: str, i: int) -> float:
    """s[i-1] と s[i] の間で折るときの「不自然さ」。小さいほど良い切れ目。

    ひらがなが続く途中は語の内部である可能性が高い（「して|る」「たくさ|ん」）。
    逆に かな→漢字/カタカナ/英数 の切り替わりは語の境目であることが多いので優先する。
    """
    prev, nxt = s[i - 1], s[i]
    if prev in _SPACES or nxt in _SPACES:
        return 0.0            # 文字起こしが空けた句切り。最優先で使う
    if prev in PUNCT:
        return 1.0
    both_hira = _is_hira(prev) and _is_hira(nxt)
    if prev in PARTICLES:
        return 12.0 if both_hira else 4.0   # 助詞でも次がひらがななら活用の途中を疑う
    if both_hira:
        return 20.0                          # 語中で割りやすい。最後の手段
    return 8.0                               # 文字種の変わり目＝語の境目になりやすい


def _can_break(s: str, i: int) -> bool:
    if i <= 0 or i >= len(s):
        return False
    if s[i] in NO_START:       # 行頭禁則
        return False
    if s[i - 1] in NO_END:     # 行末禁則
        return False
    return True


def _line_cost(length: int, per_line: int) -> float:
    """per_line は上限として扱う（超えたら不可）。短いのは緩く咎めるだけ。

    はみ出しは画面外に切れて気付きにくい事故なので、コストではなく禁止にする。
    どうしても収まらないときだけ split_lines 側が幅を1文字ずつ緩める。
    """
    if length > per_line:
        return float("inf")
    return (per_line - length) * 1.0


def house_style(text: str) -> str:
    """自動生成字幕をこのチャンネルの字幕作法へ寄せる。

    実測（納品済み68案件・2001本 vs AI生成2071本）:
      句点。 AI 7.9% -> 納品 1.7%  … 約8割が手で消されている。消してよい。
      読点、 AI 4.6% -> 納品 5.6%  … 減っていない。**消してはいけない**。
      空白   AI 27.7% -> 納品 25.3% … 句切りとして定着している。残す。
    句点は消すだけでなく空白に変える（＝そこが改行の第一候補になる）。
    人が打った文字には適用しない。呼び出し側で src=='ai' のときだけ通すこと。
    """
    import re as _re
    t = (text or "").replace("。", " ").replace("．", " ")
    t = _re.sub(r"[ 　]{2,}", " ", t)
    return t.strip()


def split_lines(text: str, per_line: int = 13, max_lines: int = 3) -> list[str]:
    """text を意味の切れ目で per_line 文字前後の行に割る。本文は絶対に捨てない。"""
    text = (text or "").strip()
    if not text:
        return []
    if "\n" in text:                     # 人が入れた改行は最優先で尊重する
        return [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(text) <= per_line:
        return [text]

    n = len(text)
    INF = float("inf")
    # dp[i] = text[:i] を行に割り切るまでの最小コスト
    dp = [INF] * (n + 1)
    prev = [-1] * (n + 1)
    nlines = [0] * (n + 1)
    dp[0] = 0.0

    for i in range(1, n + 1):
        for j in range(max(0, i - per_line - 6), i):
            if dp[j] == INF:
                continue
            if j > 0 and not _can_break(text, j):
                continue
            line = text[j:i].strip(_SPACES)
            if not line:
                continue
            cost = dp[j] + _line_cost(len(line), per_line) + (_break_penalty(text, j) if j else 0.0)
            if cost < dp[i]:
                dp[i] = cost
                prev[i] = j
                nlines[i] = nlines[j] + 1

    if dp[n] == INF:
        # per_line に収めつつ禁則を守る割り方が存在しない。
        # 本文を削るのではなく、幅を1文字ずつ緩めて再挑戦する。
        if per_line < n:
            return split_lines(text, per_line + 1, max_lines)
        return [text]

    lines, i = [], n
    while i > 0:
        j = prev[i]
        lines.append(text[j:i].strip(_SPACES))
        i = j
    lines.reverse()

    # 行数超過。文字を捨てるのではなく、1行あたりを広げて割り直す（旧実装は捨てていた）。
    if max_lines and len(lines) > max_lines:
        widened = -(-n // max_lines)     # 切り上げ
        if widened > per_line:
            return split_lines(text, widened, 0)
    return lines


def wrap_jp(text: str, per_line: int = 13, max_lines: int = 3, sep: str = r"\N") -> str:
    """ASS 用に \\N 区切りで返す（既存の wrap_jp と同じ呼び出し方）。"""
    return sep.join(split_lines(text, per_line, max_lines))


def _selftest() -> None:
    # 納品物で実際に割れていたやつ。空白の位置で折れること。
    got = split_lines("なんか時代終わったね あの子は", 13, 3)
    assert got == ["なんか時代終わったね", "あの子は"], got

    # 短い行はそのまま
    assert split_lines("おすすめのAV女優", 13, 3) == ["おすすめのAV女優"]

    # 行頭禁則: 小書き仮名・長音・句読点を行頭に置かない
    for t in ("そうだよなあれはちょっとね、まあまあまあまあ",
              "あいつはほんとうにばかだったんだよねーそうそう",
              "これはやばいっしょほんとうにやばいってばっしょ"):
        for ln in split_lines(t, 11, 4)[1:]:
            assert ln[0] not in NO_START, (t, ln)

    # 本文を落とさない（旧実装は max_lines で切り捨てていた）
    long = "あ" * 60
    assert "".join(split_lines(long, 13, 3)) == long

    # 人が入れた改行はそのまま尊重
    assert split_lines("うえ\nした", 13, 3) == ["うえ", "した"]

    # 空白は行内に残しつつ、行頭・行末には残さない
    for ln in split_lines("いやー 強奪した時はね もう", 11, 3):
        assert ln == ln.strip(), repr(ln)

    # ハウススタイル: 句点は消す / 読点は残す（実測で読点は減っていないため）
    assert house_style("そうだね。まあいいや。") == "そうだね まあいいや"
    assert house_style("えーっと、これはね") == "えーっと、これはね"
    assert house_style("") == ""

    print("jp_wrap selftest: OK")


if __name__ == "__main__":
    _selftest()
