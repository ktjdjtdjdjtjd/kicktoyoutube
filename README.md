# kick-archive-cloud

Kickの配信終了を検知して、**アーカイブDL → チャット白ダンマク焼き込み → YouTube公開アップロード** を
**GitHub Actions上で完結**させる無人ツール。ローカルPC不要・実質無料（遅くてOK前提の設計）。

```
watch.yml (15分おきcron)
  └─ Kick APIでVOD一覧を巡回 → 終了済み&未処理のVODを検知
      → state/<uuid>.json をコミット(二重処理ガード) → process.yml をdispatch

process.yml (VODごと)
  plan     : メタ解決 + チャット全量DL + エモート画像の取得/蓄積 + セグメント計画(90分単位)
  burn ×N  : VODをDL(≤1080p, Kick上限。ランナー残量不足なら720pへ自動フォールバック) → 担当区間へダンマク焼き込み (並列, 各ジョブ6h制限内)
  assemble : セグメント結合(-c copy) → 機械検証(pix_fmt/尺) → YouTubeへアップ → state更新
```

- ダンマクは**ストリップ方式**: テキスト(白・BIZ UDPGothic・黒縁)とエモート画像(フルカラー)を
  PILで「レーンごとの横長PNG」へ事前合成し、ffmpegはレーン数ぶんのoverlayでスクロールさせる。
  速度はレーンごとに `danmaku.speed_variants`(speedへの倍率リスト・既定 1.0/0.75/1.3/0.9/1.15/0.7/1.4 を
  レーンに巡回割当)で変えるので、画面上は速いコメと遅いコメが混在して見える。`[1.0]` にすれば全て等速。
  libassを使わないためエモートが画像のまま焼け、エンコードも実測10倍速超と高速
- **GIFエモートはアニメのまま焼く**: レーンごとに位相K枚(既定4枚/5fps)のストリップ変種を作り、
  overlayのenableを時分割してループ再生。**絵文字**はBIZに無いグリフを Noto Color Emoji (CBDT)
  でカラー描画 (どちらにも無い文字は豆腐にせず落とす)
- 速度・文字/エモートサイズ・GIF位相数は config.json の `danmaku` セクションで調整可
  (speed=150px/s, font_px=60, emote_px=64 はいずれも1080p基準値。720p出力では2/3倍で適用)
- **エモートはリポジトリ直下 `emotes/` に自動蓄積**(planジョブがDL→コミット)。
  ローカルのディスク掃除で消える場所には置かない
- レーン割当は常に全メッセージ一括計算のため、セグメント結合後も流れが連続する
- フォントは実行時に google/fonts (OFL) からコミット固定+ハッシュ検証で取得
- Kick APIはCloudflare対策で curl_cffi のChrome偽装を使用（住宅回線からは実測OK。
  GitHubランナーのIPで弾かれる場合は下記「プランB」）

## セットアップ（初回だけ・PCかブラウザがあればOK）

1. **GitHubリポジトリ作成**（このフォルダの中身をリポジトリ直下にpush）
   - **public推奨**: Actions実行時間が無制限・無料（privateは月2000分＝2時間配信 約12本/月で頭打ち）
   - publicの注意点: ワークフローログ（配信タイトル等）と処理中の一時Artifact（動画は保持1日）が公開される。
     動画はどのみちYouTubeで公開するので実害は小さいが、気になるならprivate＋本数を絞る
2. **YouTubeトークンをSecretsへ登録**
   - 手元で一度だけ: `python ~/.claude/skills/stream-chat-burn/youtube_upload.py --auth-only --channel archive`
     （ブラウザで投稿先チャンネルを選ぶ → `~/.claude/secrets/youtube_token_archive.json` ができる）
   - リポジトリの Settings → Secrets and variables → Actions で
     `YT_TOKEN_JSON` = そのJSONファイルの中身（丸ごと貼り付け）
   - 任意: `DISCORD_WEBHOOK` = 完了/失敗通知先WebhookのURL
3. **config.json を調整**（対象チャンネル・画質・公開設定・タイトル雛形）
4. **動作確認**: Actionsタブ → process → Run workflow に
   slug と 過去VODのuuid を入れて手動実行（`limit_windows=20` でチャット取得を100秒分に絞った
   スモークテストも可）。成功したら watch のcronが以後自動で回る

## config.json

| キー | 意味 |
|---|---|
| channels | 監視するKickチャンネルslugの配列 |
| format_height | DL画質上限（既定1080＝Kick上限の1080p30。ランナーの残ディスクが `fallback_height` で足りる分を割れば自動的に720pへ降格。目安: 約9時間超の配信は720pになる） |
| fallback_height | 残ディスク不足時のフォールバック画質（既定720） |
| segment_seconds | 分割単位秒（5400=90分。6hジョブ制限に対する安全マージン） |
| min_end_age_minutes | 配信終了からこの分数待ってから処理開始（VOD確定待ち） |
| max_vod_age_days | これより古いVODは対象外（初回導入時の一括処理暴発を防ぐ） |
| privacy | public / unlisted / private |
| title_template ほか | `{date} {title} {channel} {url}` が使える |

## 運用メモ

- 処理済み管理は `state/<uuid>.json`（dispatched→done）。再処理したい時はこのファイルを消して
  watch を手動実行するか、process を直接 Run workflow する
- 所要時間の目安（public無料枠なので気にしなくてよい）: 2時間配信 ≒ plan 10分 + burn 60-90分×2並列 + assemble 20分
- YouTube APIクォータ: videos.insert=1600units/本、既定10,000units/日 → 1日6本まで
- cronは15分間隔指定だがGitHub側の都合で数十分遅れることがある（仕様。遅くてOK）

## プランB（GitHubランナーのIPがCloudflareに弾かれた場合）

スクリプト群はプレーンなPythonなので、無料VM（Oracle Cloud Always Free等）に
`cron */15 * * * * python watch.py` 相当を置けばそのまま動く（watch.pyのdispatch部分を
plan→burn→assemble の直列実行に差し替えるだけ）。まずはActionsで1本流して確認を。

## 開発

- ロジック回帰テスト: `python scripts/selftest.py`（ネットワーク不要）
- 実チャットの疎通テスト: `PYTHONPATH=scripts python scripts/plan.py <slug> <uuid> --out /tmp/o --limit-windows 20`
