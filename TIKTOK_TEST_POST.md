# @tateyamaclips へのテスト投稿

## この手順の範囲

- 最初はGitHub Actionsから、kicktoyoutubeがOmoshiro Moviesへ公開したコメント焼き込み済みYouTube動画のURLを渡して、盛り上がり区間の上位1本を作る。試験対象はジンギスカンの動画に限る。
- TikTokの各投稿予約にはGitHub EnvironmentのRequired reviewers承認が必要。単発テストは `tiktok-test-post`、通常投稿は別の `tiktok-autopost` environmentで確認する。
- 継続投稿の自動起動はGitHub Variables `TIKTOK_AUTOPUBLISH_ENABLED` が既定で未設定または `false` の間は停止する。繰り返し投稿はまず単発テスト完了後に検討し、投稿ごとの承認ゲートを維持する。
- `tiktok_preview=true` はモザイク済みartifactを作るだけで、R2とBufferへ送らない。
- `tiktok_test_post=true` は投稿予約まで進む単発経路。通常の監視キューは有効化しない。
- テスト自動起動は `TIKTOK_TEST_SOURCE_VOD_UUID` に指定したKick VOD UUIDだけを対象にする。YouTube動画の説明欄、完了済み `state/<UUID>.json`、動画IDが一致しない場合は停止する。
- YouTube動画はyt-dlpが `availability=public` と確認できるものだけ受け付ける。限定公開・非公開・公開状態不明の動画は停止する。
- Bufferから取得したチャンネル名またはTikTokプロフィールURLが `tateyamaclips` と一致しない場合は停止する。[Buffer Channel API](https://developers.buffer.com/types/Channel.html)
- テスト経路は当月1投稿まで。成功後は `TIKTOK_TEST_POST_ENABLED` を削除または `false` に戻す。

## 先に一度だけ設定

GitHubの **Settings → Environments** に `tiktok-test-post` environmentを作成し、Required reviewersに投稿内容を確認する担当者を追加する。通常投稿用の `tiktok-autopost` environmentにも別途Required reviewersを設定する。どちらもworkflowがGitHub APIでレビュアー保護を検証し、未設定・照会失敗時はR2/Buffer処理前に停止する。Environment承認とAPI検証があるため、各予約ごとに承認する。リポジトリはPublicなので、標準GitHub-hosted runnerの利用料は無料で、GitHub Freeでもenvironmentの承認保護を使える（[GitHub Actions billing](https://docs.github.com/en/actions/concepts/billing-and-usage)、[GitHub Environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)）。

**Settings → Secrets and variables → Actions** に次を設定する。APIキーや秘密値はチャットへ貼らない。

| 種別 | 名前 | 内容 |
| --- | --- | --- |
| Secret | `BUFFER_API_KEY` | Buffer APIキー |
| Secret | `BUFFER_TIKTOK_CHANNEL_ID` | Bufferで接続した `@tateyamaclips` のID |
| Secret | `R2_ACCOUNT_ID` | CloudflareアカウントID |
| Secret | `R2_ACCESS_KEY_ID` | 専用バケットだけのR2キー |
| Secret | `R2_SECRET_ACCESS_KEY` | 上記R2キーの秘密値 |
| Secret | `TIKTOK_OBJECT_KEY_SECRET` | 32文字以上のランダム値。作成後は変更しない |
| Variable | `R2_PUBLIC_BASE_URL` | 専用R2バケットに接続した公開HTTPSドメイン |
| Variable | `TIKTOK_TEST_POST_ENABLED` | テスト直前だけ `true`。テスト後は削除または `false` |
| Variable | `TIKTOK_TEST_SOURCE_VOD_UUID` | テスト対象にする単一のKick VOD UUID。テスト後は削除 |
| Variable | `TIKTOK_SOURCE_CHANNEL_ID` | Omoshiro Moviesの正確なYouTubeチャンネルID。チャンネル名から推測しない |
| Variable | `TIKTOK_AUTOPUBLISH_ENABLED` | 現状は未設定または `false` を維持。繰り返し投稿の検証・承認後にだけ `true` |

現在、必要なGitHub Variables/Secretsと両EnvironmentのRequired reviewersが未設定のため、preview・テスト投稿の実行はまだブロックされている。テスト時は `TIKTOK_TEST_POST_ENABLED=true` と選定済みVODの `TIKTOK_TEST_SOURCE_VOD_UUID` を設定し、最初のゴールは `tiktok_test_post` による月1回のテスト予約とする。通常投稿フラグは有効化しない。

Cloudflare R2に専用バケット `zingisukan-tiktok-public` を作る。既存バケット `zingisukan-selection` は公開しない。専用バケットにはモザイク済み動画だけを入れ、オブジェクト一覧を公開せず、`tiktok/` prefixを7日後に削除するLifecycle ruleと、バケット限定の書き込みキーを設定する。

単発テストでは、追加費用を避けるためR2のPublic Development URL（`r2.dev`）を使える。Cloudflareはこれを開発用途向け・レート制限ありとしているため、継続運用では既に所有しているドメインをR2へ接続する。新規ドメインや有料プランは契約しない（[Cloudflare R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/)）。Bufferは投稿時までアクセス可能な公開HTTPSメディアURLを必要とするため、期限付きURLは使わない（[Buffer media hosting](https://developers.buffer.com/guides/hosting-media.html)）。

モザイクは人物検出後、画面内で最大の人物を配信者本人と仮定して残す方式で、顔による本人確認ではない。投稿前にプレビューを全編確認し、本人以外が残る・本人が隠れる・人物を検出できない場合は承認せず停止する。

## テスト手順

1. `shorts_prep` を手動実行し、Omoshiro Moviesに公開済みのジンギスカンYouTube動画URLを指定して `platform=youtube`、`tiktok_preview=true`、`tiktok_test_post=false`、`tiktok_auto_post=false` にする。登録した `TIKTOK_SOURCE_CHANNEL_ID` と一致し、公開状態が確認された動画だけ受け付ける。説明欄のKick VOD URLと完了済みstateのYouTube動画IDを照合し、候補区間のチャットは同一Kick VODから取得する。実際の動画クリップはコメント焼き込み済みYouTube公開動画から切り出す。候補は1本、45〜90秒。字幕・見出し・モザイクをartifactで確認する。
2. その映像でよければ、同じYouTube URLを指定して `platform=youtube`、`tiktok_preview=false`、`tiktok_test_post=true` にする。処理は1本だけ行い、Gemini APIは呼ばない。
3. 生成された `shortpack` artifactを確認する。jobは `tiktok-test-post` environmentの必須レビュアー承認待ちになる。承認後、workflowがenvironmentにレビュアー保護が設定されていることもAPIで再確認し、Bufferへ30分後の公開予約を送る。承認しなければ投稿処理は開始しない。
4. 投稿予約後、`TIKTOK_TEST_POST_ENABLED` と `TIKTOK_TEST_SOURCE_VOD_UUID` を削除する。テスト経路を停止し、次の投稿を対象にしない。

`tiktok_auto_post` は `TIKTOK_AUTOPUBLISH_ENABLED=true` と `tiktok-autopost` Required reviewersの両方を満たす場合のみ選択でき、承認された実行ごとに予約する。最初のテスト投稿前はこの変数を有効化しない。

GitHub artifactはPublic repositoryのread accessを持つユーザーがダウンロードできるため、一般公開扱いで確認する。artifactにはモザイク済み最終動画と見出しmanifestだけを1日保存し、未加工素材を含めない（[GitHub artifact download](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)）。

Bufferの現行TikTok投稿メタデータAPIには公開範囲の指定項目がない（[TikTok metadata](https://developers.buffer.com/types/TikTokPostMetadataInput.html)）。このテストはTikTok側のアカウント設定に従うので、公開範囲を確認してからenvironment承認する。BufferのTikTok接続・予約投稿は新プランで利用できる。Freeプランでは同時に3チャンネル、各チャンネル10件まで予約でき、APIキーと月3,000リクエストも含まれる（[Buffer pricing](https://buffer.com/pricing)、[TikTok with Buffer](https://support.buffer.com/en-us/articles/using-tiktok-with-buffer-oGEroY9Of2)、[Buffer API plans](https://support.buffer.com/en-us/articles/what-is-buffers-api-GtIYIQilz5)）。既存アカウントがLegacy planの場合は、アップグレードせずに停止して費用を確認する。

## 月額上限

全体の月額上限は **1,000円**。想定構成はPublic repositoryの標準runner（追加0円）、Buffer Free（0円）、Cloudflare R2のFree tier内（追加0円）、新規ドメインなし。R2 Standardの無料枠は月10GB-monthの保管、月100万回の書き込み、1,000万回の読み込みで、外向き通信料は無料。25本/月・最大1GB・7日保管なら平均約5.8GB-monthなので、他のR2利用が無料枠を使い切っていない場合、動画保管の追加費用は0円の見込み（[R2 pricing](https://developers.cloudflare.com/r2/pricing/)）。

Bufferの利用中プラン、R2の既存使用量、既存ドメインの費用はここから確認できない。これらを含む総額が1,000円以内と確認できるまで、テスト投稿用variableを有効にしない。有料プラン・新規ドメインが必要だと分かった場合は、購入・契約せず停止する。Lifecycle ruleはGitHubから確認しておらず、R2側で手動設定が必要。
