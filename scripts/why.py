"""この配信いま何がどうなってる？ を1コマンドで答える (読み取り専用)。

    python scripts/why.py https://www.twitch.tv/videos/2866728770
    python scripts/why.py 2866728770

state/<uuid>.json ・ 自動処理から外れている理由 ・ 元VODがまだ生きているか を
まとめて出す。以前は state を開く→watch.py の除外条件を読む→yt-dlp で生存確認…と
6ターン使っていた調査。答えは毎回同じ3点なので機械化した。
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# watch.py:142 の除外条件。ここが増えたら追随する。
BLOCKED_PREFIXES = ("done", "skipped", "needs-review")


def extract_uuid(arg):
    """URL でも素の uuid でも受ける。"""
    m = re.search(r"(?:videos/|/video/)([\w-]+)", arg)
    return m.group(1) if m else arg.strip()


def read_state(uuid, ref="origin/main"):
    """state はリモートが正 (ローカルの作業ツリーは古いことがある)。"""
    p = subprocess.run(["git", "-C", ROOT, "show", f"{ref}:state/{uuid}.json"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return json.loads(p.stdout)


def source_alive(slug, uuid, platform):
    """元配信がまだ取得できるか。消えていたら復旧不能なので最優先で見る。"""
    try:
        if platform == "twitch":
            import twitch_chat_fetch as T
            vs = T.get_channel_videos(slug)
        else:
            import kick_api
            vs = kick_api.get_channel_videos(slug)
    except Exception as e:
        return None, f"確認できず ({e})"
    ids = [(v.get("video") or {}).get("uuid") for v in vs]
    return uuid in ids, f"チャンネルに残っているVOD {len(ids)}本"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    uuid = extract_uuid(sys.argv[1])
    st = read_state(uuid)
    if st is None:
        print(f"{uuid}: state なし = 未処理。次のwatch実行で拾われる可能性がある。")
        return

    slug = st.get("slug", "?")
    status = str(st.get("status", ""))
    print(f"uuid    : {uuid}")
    print(f"slug    : {slug}")
    print(f"status  : {status or '(なし)'}")
    for k in ("title", "yt_url", "review_reason"):
        if st.get(k):
            print(f"{k:8s}: {st[k]}")

    ch = st.get("chapters")
    if isinstance(ch, dict):
        print(f"chapters: {ch.get('result')} n={ch.get('n')} at={ch.get('at')}")
    elif "chapters" in st:
        print(f"chapters: {ch}")
    else:
        print("chapters: 未生成")

    if status.startswith(BLOCKED_PREFIXES):
        print(f"\n→ status が {status!r} なので watch.py は二度と自動投入しない。")
        print("   再処理するなら state/<uuid>.json を消す (README:69)。push 承認が要る。")
    elif status.startswith("dispatched"):
        print(f"\n→ 処理中 (retries={st.get('retries', 0)})。")
    else:
        print("\n→ 自動投入の対象。")

    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    platform = (cfg.get("channel_settings", {}).get(slug, {})).get("platform", "kick")
    alive, note = source_alive(slug, uuid, platform)
    if alive is None:
        print(f"元配信  : {note}")
    elif alive:
        print(f"元配信  : {platform} にまだ在る ({note})")
    else:
        print(f"元配信  : {platform} から消えている = 復旧不能 ({note})")


if __name__ == "__main__":
    main()
