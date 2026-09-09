# 本番接続 2026-09-08

URL: https://zingisukan-selection.kirinuki-dashboard.workers.dev
Cloudflare account: afdb71637c2c3b09de9ad39ca57f1527
Worker / private R2 bucket: zingisukan-selection
承認: deployment-05bf16b30a（ユーザーの明示指示）

完了: R2有効化、OAuth、bucket作成、Worker配置、秘密設定、GitHub main反映、BOARD_ENABLED=true。
本番検証: 未認証HTML/API/media=401、認証API=200、実動画Range=206、同一候補の再追加で重複なし、判定/±10秒保存と再読込（元状態に復元）。
本番候補: 旧VOD-A a9380を前後30秒付き200秒で再生成して1件追加。旧Artifactの判定は移行していない。

初回Actionsテスト: https://github.com/ktjdjtdjdjtjd/kicktoyoutube/actions/runs/34228211890
候補抽出ステップでKick HTTP 429が継続。負荷を重ねないため手動テストを停止。Append padded previewsは未到達で、Actions経由の自動追加は未検証。BOARD_ENABLED=trueだが、上流の取得制限解消または既存チャット再利用の対応が必要。
旧47件の一括移行とiPhone実機確認は未実施。既存Artifactは維持。

ユーザー名 kick。パスワードのローカル保管先:
C:/Users/KeNEe/.claude/secrets/zingisukan-selection-auth_pass.txt
送信用値も同ディレクトリ。値をログ・応答へ出さない。
WranglerはXDG_CONFIG_HOME=C:/Users/KeNEe/.claude/secrets/cloudflare-config で使用。

修正: urllib既定User-AgentがCloudflare 1010で拒否された。kicktoyoutube-board/1.0を明示し本番成功。

2026-09-09: 旧47候補を追加。process.ymlのboardジョブはplanのchatpackを再利用。board_replay run 34243956659で新規追加中。全件一覧はページ20件・同時読込3件で修正。6 API tests PASS。ブラウザ接続なしのため本ターン画面検証不可。

最終確認: board_replay 34243956659 success。55件(旧47+新8)、新MP4 Range206。定期追加はprocess.plan成功後のboard job。既存watchが新規配信を選んだ時に動く。

2026-09-09 採用から編集: scripts/board_edit_queue.pyで本番採用3件をローカル永続キューwork/stream/_board_edit_queue/queue.jsonへ取り込み。単体テスト3件PASS。候補IDと補正後区間で重複防止。Codex heartbeat automation-2 ACTIVE、15分間隔、target_thread_id=01a07fa4-9572-7921-aefd-215bf72df1f1を設定ファイルで確認。zingisukan-shortで1件ずつ編集し検証・ローカル納品後に完了記録する。今回の3件はqueuedで、動画編集・レンダーはまだ未実施。PC/Codex稼働が必要。YouTube投稿は自動化対象外。
