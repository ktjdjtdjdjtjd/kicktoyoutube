# 常設のクリップ選定ボード

既存 kicktoyoutube の拡張。監視 → shorts_pick → board_publish → Cloudflare Worker/R2。
本人用。HTML・候補JSON・MP4の全ルートを認証し、R2は非公開のまま使う。
既存 kirinuki-dashboard と同じBasic認証方式を再利用し、既存Workerは変更しない。

## 振る舞い

- 新しいジンギスカン配信の候補だけを追加。既存watchのshorts_channelsに接続。
- 配信URL＋元の開始/終了から固定IDを生成。再実行しても同じ候補は増えない。
- 候補全体に前後30秒を加えた低画質H.264/AAC MP4を生成。全デコード・尺・形式チェック後に送信。
- 採用・保留・却下、開始/終了±10秒。余白の外へは変更しない。
- 「この区間を再生」で調整後の開始から再生し、終了位置で停止する。
- 候補と判定は別オブジェクト。追加APIに判定の更新権限はない。
- 保存はETagで競合検出。別端末の変更を黙って上書きしない。
- 開いている画面は60秒ごとに新着を取得。再生中のvideoノードは保持する。
- Actions側の追加失敗は既存下書き工程を続けた後に失敗として通知。再実行で追加済みをスキップ。

## 本番接続の残件

現在は未デプロイ。2026-09-08時点でwranglerは未認証。
以下は操作手順であり、実行済みという意味ではない。

1. Cloudflareの対象アカウントへwrangler login。R2の利用条件/課金が新しく必要なら利用者確認。
2. 対象アカウントを確認して非公開R2 bucket `zingisukan-selection` を作成。
3. `wrangler.toml` のWorkerをデプロイ。未設定時は503で内容を返さない。
4. Worker secrets `AUTH_PASS` と `INGEST_TOKEN` を設定する。ブラウザのユーザー名は `kick`。
   値のローカル保管は C:/Users/KeNEe/.claude/secrets/ のみ。HTML、設定ファイル、引数、ログに埋め込まない。
5. GitHub repository secret `BOARD_INGEST_TOKEN` に同じ送信用値を登録。
   Variables `BOARD_URL`（WorkerのHTTPS origin）、`BOARD_ENABLED=true` を設定。
6. この変更をレビューしてGitHubへ反映。未認証GETが401、認証GETが200、未認証mediaが401であることを確認。
7. ジンギスカンのVOD URLでshorts_prepを手動実行して本番の追加→再生→判定保存→再実行の重複なしを確認。
   定期起動は既存watchが担当し、別のcronは作らない。

新規の料金プラン契約、公開アクセスへの変更、認証を迂回するフラグは実施しない。
GitHub送信資格情報は追加専用で、ブラウザ閲覧/判定保存には使えない。

## 旧47候補の扱い

既存Artifactの判定データとHTMLは変更しない。公開切替前に最新の判定をエクスポートし、
新しい固定IDへの対応表と共に移行する必要がある（未実施）。旧プレビューは候補全体を含まない
ものが21本あるため、そのままクラウド版の調整用動画として流用しない。元動画から余白付きで再生成する。
Artifactにはこの環境からの更新/DBエクスポート機能がなく、同期/移行を完了扱いにしない。

## 検証

```
node --test cloudboard/api.test.mjs
python -m unittest discover -s scripts -p test_board_publish.py
python scripts/selftest.py
# cloudboardで実行（既存インストールのwranglerを利用）
wrangler deploy --dry-run
```

実施済み: 追加API二重PUT（duplicate false→true）、R2 Range 206/100bytes、
ブラウザで採用・開始-10・終了+10（30→50秒）の保存/再読込復元・調整区間の再生。
既存selftestはignoredフォント NotoColorEmoji.ttf を既存checkoutから複製してALL OK。
新規API4件、Python2件、Worker bundle dry-run PASS。
ローカルテスト用認証値は本番用の秘密ではなく、本番へ設定しない。

一次資料:
- https://developers.cloudflare.com/r2/api/workers/workers-api-reference/
- https://developers.cloudflare.com/r2/get-started/workers-api/

未検証: 本番Cloudflare/GitHub接続、iPhone実機、旧Artifactからのデータ移行。
