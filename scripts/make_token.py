"""YouTubeトークン作成 (ローカル実行用・ブラウザが開く)。

    # 説明欄編集も可能なフルトークン (チャプター自動化に必要)
    python scripts/make_token.py --out yt_token_full.json

    # 投稿先チャンネルを増やす場合も同じ (ブラウザで対象チャンネルを選ぶ)
    python scripts/make_token.py --out yt_token_<name>.json

出力されたJSONの中身を、リポジトリの Secret (YT_TOKEN_JSON など) に貼り付ける。
"""
import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
DEFAULT_CLIENT = Path.home() / ".claude" / "secrets" / "youtube_credentials.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default=str(DEFAULT_CLIENT),
                    help="OAuthクライアントJSON (Desktop)")
    ap.add_argument("--out", default="yt_token_full.json")
    a = ap.parse_args()
    flow = InstalledAppFlow.from_client_secrets_file(a.client, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    Path(a.out).write_text(creds.to_json(), encoding="utf-8")
    print(f"saved: {a.out}")
    print("この中身を GitHub Secret YT_TOKEN_JSON に貼り付けてください")


if __name__ == "__main__":
    main()
