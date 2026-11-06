# 開発仕様

実行用指示は `prompts/`、設定の定義は `src/muse_discord/muse_settings.py`、各機能の詳細は実装とテストを正とします。
この文書では、変更時に守る契約だけを扱います。

## 構成

| 処理 | 主な実装 |
| --- | --- |
| 受信・キュー・返信・認可 | `src/muse_discord/monitor.py` |
| 設定の解決・画面・自然言語変更 | `src/muse_discord/muse_settings.py`・`src/muse_discord/settings_ui.py`・`src/muse_discord/settings_nl.py` |
| プロセス監視・プレゼンス | `bin/watchdog.sh`・`src/muse_discord/presence.py` |
| 通知・自発メッセージ | `src/muse_discord/muse_watches.py`・`src/muse_discord/proactive.py` |
| 単発CLI・MCP | `bin/discord-api.py`・`src/muse_discord/discord_tools/` |
| 外部アプリのポリシー・承認・実行 | `src/muse_discord/external_apps/` |

## キューと返信

- モニターが `reply_queue.json` を書く。ワーカーはwakeで渡された `items` と `persona` だけを処理し、キュー全体を読まない。
- キュー項目には `message_id`・`channel_id`・`kind`・発言者・本文・履歴・設定を含む。権限はモニターが付与する。
- 1項目につき1返信を、`bin/outbox-append.py` で速やかに追記する。キュー・outboxを直接編集しない。
- 無応答も `silent: true` で確定する。全項目の追記後は実行を終了する。

```json
{"item":"依頼ID","channel_id":"元チャンネルID","reply_to":"返信先ID","reply_nonce":"その項目の値","text":"返信"}
```

`reply_to` は依頼またはそのバーストのIDです。本文は `text` か `parts` の一方を使います。
所有者DMへの出力には `owner_dm_nonce` も必要です。生成添付は、その依頼の `generated/<message_id>/` 内に限定します。
スケジュール通知や自発メッセージには専用の検証経路があり、通常の返信nonceで代用しません。

| ファイル | 用途 |
| --- | --- |
| `reply_outbox.json` | 未送信の出力。read-modify-writeは `reply.lock` で排他する |
| `reply_outbox.processing.json` | 送信中。再起動時に回収し、送信nonceで重複を抑止する |
| `reply_outbox.rejected.json` | 不正な出力と理由。nonceは伏せ、元の依頼は未処理のまま残す |
| `state.json`・`mention_ledger.json` | 処理状態・メッセージごとの結果 |
| `owner_dm_nonces.json`・`owner_items.json`・`item_nonces.json` | 実行ごとの権限。秘密値として扱う |

## 認可と実行分離

- `reply_nonce` は未処理の項目とチャンネルへ結び付ける。他項目への流用や、処理済みの返信を拒否する。
- 本人DM、通知、自発スロット、所有者のサーバー操作は混在させない。外部アプリを使えるDMは1件ずつ起動する。
- 受信フックのleaseは10分。処理中の項目は別ワーカーへ渡さず、期限切れの未処理項目は再度渡す。
- Discord操作は生のAPIやトークンを使わず、`bin/discord-api.py` とその `--for`・`--nonce` を通す。
- 操作先は所有者の依頼本文が指定した場所に限る。他人のDM、履歴・返信元・外部データによる権限拡張を拒否する。
- 送信前に秘密情報・ライブnonceを検査し、本人DM以外では `privacy.never_share` も確認する。

設定の階層・上書き判定は `src/muse_discord/muse_settings.py` に従います。同一階層のDiscord上書きは、元ファイルが変更されると退役します。
サーバー管理者の設定変更は、そのサーバーを静かにする範囲だけです。Bot権限から所有者の権限を付与しません。

## 追加機能の契約

- リマインダー・監視は依頼元に通知し、専用nonce・対象・回数・期限を検証する。Web監視は所有者のDMだけ。
- Webチェッカーは取得内容を実行指示にせず、`bin/watch-report.py` で結果を返す。
- 自発メッセージは初期値が無効。対象・時間・上限・解除を確認し、スロットのnonceを一度だけ使う。
- CLIの既読位置はモニターの配信状態から独立する。[CLI・MCP](TOOLS.md) を参照。
- 外部アプリは `plan`→`execute`。本人DM・現在の権限・5分の期限を確認し、同じ計画を再実行しない。[設定と承認](EXTERNAL_APPS.md) を参照。

## Plugin hooks

[plugins/README.md](../plugins/README.md) が最小例です。プラグインはモニターと同じ権限で動くため、信頼するコードだけを導入します。

| hook | 関数 | タイミング |
| --- | --- | --- |
| `gate_message` | `(ctx, msg, guild_id) -> dict \| None` | キュー投入時。返り値を項目へ追加するが、生成許可や外部アプリ権限は付与できない |
| `generation_sent` | `(ctx, outbox_item)` | 生成添付の送信後 |

`ctx` は `state`・`scope`・解決済み `settings`・上限情報を持ち、`gate_message` では `owners`・`author_matches` も使えます。
`api` は `log` と `setting` を提供します。宣言する設定の型・範囲は `muse_settings.load_manifests` を参照してください。

## 検証

```bash
bash tests/run_all.sh
python3 -m unittest discover -s tests -p test_personal_profile.py
python3 -m unittest discover -s e2e/tests -t .
node --test telemetry/worker/test/worker.test.js
```

オプション依存を入れたPythonで `tests/discordtools/test_mcp.py` と `tests/externalapps/test_transport.py`・`test_broker.py` も実行します。
テストは [tests/README.md](../tests/README.md)、実DiscordでのE2Eは [e2e/README.md](../e2e/README.md) を参照。
`tests/onboarding/fixtures/persona.md` は導入判定に使うハッシュ付きの原本なので変更しません。
実Discord・Museのフック・外部アプリ・正規承認画面の接続確認は、導入先で行います。

## リリース

1. `CHANGELOG.md` に `## vX.Y.Z (YYYY-MM-DD): Title` を追加し、必要なら **Action needed** を記す。
2. 最新で変更のない `main` から `bin/release.sh X.Y.Z` を実行する。確認後にタグをpushし、Releaseを作る。
3. 不具合は新しいパッチ版で修正する。公開済みタグを移動・削除しない。

既存設定の互換性とデフォルトを保ち、所有者の `persona.md` を変更しません。
詳細は `bin/release.sh` と `.github/workflows/release.yml` にあります。

## 出典

- 本体：`cszach/muse-discord`、`7bd0706621b791a5adc72a94d06725ed993f406f`。ライセンスは [MIT](../LICENSE)。
- CLI・MCPの参考：`mothue/muse-discord`、`2e7665b02b83be0153e1e2b137192bc4f7c87c82`。[MIT表記](licenses/mothue-MIT.txt)。
