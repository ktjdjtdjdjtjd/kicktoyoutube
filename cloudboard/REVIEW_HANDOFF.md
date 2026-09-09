# 編集済み・FB機能の引継ぎ 2026-09-09
目的: Cloudflareログイン付きボードに編集版3本とテキストFB送信。版ごとの履歴、原本設定で修正して返す。
実装: api.mjs/review.mjs/index.html、scripts/board_review.py、FEEDBACK_WORKFLOW.md。
検証: API15件、実MP4を使う390pxブラウザ再生/FB再送/版選択/XSS/ETag更新1件、Python4件PASS。Worker dry-run PASS。本番未配置・未アップロード。
自動処理: automation-2を既存15分間隔のままFB優先へ更新済み。API404は未配置として静かに従来採用処理へ。PC/Codexの稼働が必要。
承認待ち動画: video-2d9fdcc867 (海苔アップ60s)、video-e860a9cfc6 (焼肉50s)、video-00efee0dbb (マスク90s)。3本ともdraft。音声QA未了をcompleteへ偽装しない。
本番反映手順（人の明示承認後）:
1. python scripts/board_review.py deploy で現行codehashの承認ID確認。承認された対象だけapproval_gate approve --yes --human-instructedを使い、同コマンド再実行。
2. work/stream/_board_edit_queue/queue.jsonの3jobについて各project/DRAFT.jsonからvideo/specを取得し board_review.py upload candidate_id --video VIDEO --spec SPEC。対象の既存承認IDを人の指示に基づいて承認してから送信。
3. 認証付き /api/edits が3件、動画GET/HEAD/Rangeが200/206、未認証401を検証。ユーザーのボードで編集済み、実動画再生、FBフォームを確認。
4. UIの既定date表示等が変更されたらdeployment hashは変わる。古い承認IDを流用しない。
未検証: Workers実環境での最大ファイル送信(最大89MB)。必要時は元を残してレビュー用縮小版を作り全デコード後に別hashで承認する。UI/APIのE2Eは隔離テスト、本番FBをテストで生成していない。
承認ルール: external-publishing.mdにより私有Cloudflareへの送信にもrequire_approvalを置いた。自動での再掲載は未承認なら停止する。userの今後FB返却希望だけを根拠にゲート例外を作っていない。
却下案: Cloudflare Worker内で既存Python/ffmpegスキルをそのまま実行すること。受付/保存と編集計算を分離する。クラウド実行基盤やAI API課金はまだ導入しない。

現行deployment承認ID: deployment-831f4b0cd0。UI日時簡略化・ハッシュ非表示まで含む。

2026-09-09 本番反映完了: ユーザー「やってみましょ」で4対象承認。3本upload済み、一覧/HEAD/Range/未認証PASS。マスク初回503は再送成功。費用はCOST_REPORT.md。PC不要基盤は未導入。
