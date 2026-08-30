"""投稿済み動画の一括レトロフィット (workflow_dispatch / maintenance push から1回実行)。

1. 説明欄のタイムスタンプ節を新形式へ再整形
   (1行に連結されてしまった章の分解 + ゼロ埋め MM:SS / HH:MM:SS)
2. 220ninimaru の動画タイトル 【ににまる】→【かつき】、説明欄・タグの名称も更新
3. chapters が skipped-too-few だった動画は chapters キーを外して再キュー
   (旧パーサが1行連結出力を捨てていたケースの救済)

    python retrofit_format.py [--config config.json] [--dry-run]
"""
import argparse
import re
import json
import os
import sys
from pathlib import Path

import kick_api
from chapters import extract_pairs, inject_description
from repo_state import STATE_DIR, commit_paths

HEAD_MARKER = "タイムスタンプ▽"
TAIL_MARKER = "元配信▽"


def get_youtube(token_env, cache={}):
    if token_env in cache:
        return cache[token_env]
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    info = json.loads(os.environ[token_env])
    creds = Credentials.from_authorized_user_info(info)  # 付与済みスコープをそのまま使う
    if not creds.valid:
        creds.refresh(Request())
    cache[token_env] = build("youtube", "v3", credentials=creds)
    return cache[token_env]


def reformat_description(desc, slug):
    """タイムスタンプ節の再整形 + (ににまる動画のみ) 名称更新。"""
    if HEAD_MARKER in desc and TAIL_MARKER in desc:
        head, rest = desc.split(HEAD_MARKER, 1)
        block, tail = rest.split(TAIL_MARKER, 1)
        pairs = extract_pairs(block)
        if len(pairs) >= 3:
            pairs = sorted(dict.fromkeys(pairs))
            desc = inject_description(f"{head}{HEAD_MARKER}\n\n\n{TAIL_MARKER}{tail}",
                                      pairs)
    if slug == "220ninimaru":
        desc = desc.replace("ににまるのKICK配信", "かつきのKICK配信")
        if "#かつき" not in desc:
            desc = desc.replace("#ににまる", "#かつき #ににまる")
    if slug == "zingisukan2525":
        # 検索KW反映 (2026-08-30): 冒頭文とハッシュタグを新テンプレへ
        desc = desc.replace(
            "ジンギスカンのKICK配信の録画アーカイブです。",
            "ニコ生出身の配信者ジンギスカンのKick配信 録画アーカイブです。\n"
            "毎日の配信をフルで残しています。")
        if "#ニコ生" not in desc:
            desc = desc.replace("#キッカーズ #ジンギスカン",
                                "#ジンギスカン #Kick配信 #ニコ生 #キッカーズ")
        if "sub_confirmation" not in desc:
            # 登録導線をハッシュタグ行の直前へ (2026-08-31)
            sub = ("チャンネル登録▽\n"
                   "https://www.youtube.com/channel/"
                   "UC9XW9n39Ai_Dcpfkztewgpw?sub_confirmation=1")
            if "\n#ジンギスカン" in desc:
                desc = desc.replace("\n#ジンギスカン",
                                    "\n" + sub + "\n\n#ジンギスカン", 1)
            else:
                desc = desc.rstrip() + "\n\n" + sub
    return desc


def retitle(title, slug):
    if slug == "220ninimaru" and title.startswith("【ににまる】"):
        return "【かつき】" + title[len("【ににまる】"):]
    if slug == "zingisukan2525" and "Kick配信" not in title:
        # 【2026/08/23】→【Kick配信 2026/08/23】 (分割パートの（1/2）等は後置のまま保たれる)
        title = re.sub(r"【(\d{4}/\d{2}/\d{2})】", r"【Kick配信 \1】", title, count=1)
    return title


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = kick_api.load_config(a.config)
    csettings = cfg.get("channel_settings") or {}

    requeue_paths = []
    updated = skipped = 0
    for p in sorted(STATE_DIR.glob("*.json")):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        slug = st.get("slug")
        yt_url = st.get("yt_url") or ""
        if st.get("status") != "done" or not slug or "v=" not in yt_url:
            continue

        # 旧パーサに捨てられた章の再キュー
        if (st.get("chapters") or {}).get("result") == "skipped-too-few":
            st.pop("chapters", None)
            p.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            requeue_paths.append(p)
            print(f"requeue chapters: {st.get('title')}")

        video_id = yt_url.split("v=")[-1]
        token_env = csettings.get(slug, {}).get("yt_token_env", "YT_TOKEN_JSON")
        yt = get_youtube(token_env)
        items = yt.videos().list(part="snippet", id=video_id).execute().get("items", [])
        if not items:
            print(f"not found (deleted?): {video_id} {st.get('title')}")
            continue
        snippet = items[0]["snippet"]
        new_title = retitle(snippet.get("title", ""), slug)
        new_desc = reformat_description(snippet.get("description", ""), slug)
        tags = snippet.get("tags") or []
        if slug == "220ninimaru" and "かつき" not in tags:
            tags = ["かつき"] + tags
        if slug == "zingisukan2525" and "ニコ生" not in tags:
            tags = (csettings.get(slug, {}).get("tags") or tags) or tags
        changed = (new_title != snippet.get("title")
                   or new_desc != snippet.get("description")
                   or tags != (snippet.get("tags") or []))
        if not changed:
            skipped += 1
            continue
        print(f"update: {video_id} {new_title}")
        if a.dry_run:
            continue
        snippet["title"] = new_title
        snippet["description"] = new_desc
        snippet["tags"] = tags
        yt.videos().update(part="snippet",
                           body={"id": video_id, "snippet": snippet}).execute()
        updated += 1

    if requeue_paths and not a.dry_run:
        commit_paths(requeue_paths,
                     f"state: requeue chapters for {len(requeue_paths)} vod(s)",
                     fatal=False)
    print(f"done: updated={updated} unchanged={skipped} requeued={len(requeue_paths)}")


if __name__ == "__main__":
    main()
