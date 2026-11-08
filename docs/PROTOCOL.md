# 開発仕様

実行用指示は `prompts/`、設定の型・初期値・制約の定義は `src/muse_discord/settings_schema.py`、各機能の詳細は実装とテストを正とします。
この文書では、変更時に守る契約だけを扱います。

所有者の在籍確認・通知・返信承認は、起動時に作る各サービスがメモリー上の状態を保持します。
`monitor.py` はサービスを接続し、永続状態の復元とイベントの受け渡しを行います。
更新通知のファイルパスと所有者一覧は、利用時に提供関数から取得します。
返信・所有者操作用nonceの保存済みキャッシュと再発行時刻は `owner/capabilities.py` が保持し、
発行・返信照合・送信前の秘密情報検査で同じ保存状態を参照します。
入力中表示の対象・最終送信時刻・ロックは `messaging/typing.py` が保持します。
送信と分割送信は起動時に接続し、入力中表示スレッドと返信処理が同じサービスを使います。
outboxの処理中項目・失敗回数・新規返信の到着検知は `OutboxProcessor` が保持します。
一回の処理では保存先の組を固定し、処理間で保存先を変更した場合は次回から参照します。
所有者DMからの記憶保存と保存先がない場合の通知抑制は `OwnerMemory` が保持し、書き込みごとに保存先を確定します。

## 構成

| 処理 | 主な実装 |
| --- | --- |
| REST巡回・時間配分・定期ジョブの実行管理 | `src/muse_discord/rest_scheduler.py` |
| 起動・イベント処理・各機能の接続 | `src/muse_discord/monitor.py` |
| 接続CLIの引数解析と実行先の選択 | `bin/connect-discord.py` |
| 接続用の入力判別・設定保存・本人確認・参加確認・結果表示 | `src/muse_discord/setup/` |
| CLIのPython環境選択 | `bin/setup_common.py` |
| 匿名利用統計の公開API | `src/muse_discord/telemetry.py` |
| 匿名レポートの項目選別・集計 | `src/muse_discord/usage_stats/payload.py` |
| 設定からの列挙値・変更件数の抽出 | `src/muse_discord/usage_stats/settings_metrics.py` |
| 利用統計の保存・ID・送信・日次制御 | `src/muse_discord/usage_stats/storage.py`・`transport.py`・`recorder.py` |
| ワーカー向けの返信先・文脈・設定カードの作成 | `src/muse_discord/worker_context.py` |
| チャンネル・サーバー・探索メタデータの共有状態 | `src/muse_discord/directory/state.py` |
| チャンネル・スレッド探索結果の採用と公開 | `src/muse_discord/directory/refresh.py` |
| 設定の再読み込み・古い上書きの整理・所有者状態・スレッド間の変更引き渡し | `src/muse_discord/settings_watch.py` |
| メッセージの処理結果・診断理由の永続化と観測した名前のキャッシュ | `src/muse_discord/message_ledger.py` |
| 受信拒否・送信権限不足の記録と重複ログの抑制 | `src/muse_discord/receiving/audit.py` |
| 受信バーストのキュー登録・再起動時の未登録項目の復旧 | `src/muse_discord/receiving/staging.py` |
| 受信内容の検査・連投全体の隔離・破損記録の退避 | `src/muse_discord/receiving/quarantine.py` |
| REST・Gateway受信の分類・確認位置・重複排除 | `src/muse_discord/receiving/` |
| 連投の集約・到着順序・登録失敗の再試行 | `src/muse_discord/bursts.py` |
| 受信バーストの検証・キュー項目の組み立て | `src/muse_discord/ingest.py` |
| 依頼のメタデータ・時刻・連投全体の完了記録 | `src/muse_discord/reply_state.py` |
| 通常返信の依頼・送信先・nonceの対応確認と指示不整合の診断 | `src/muse_discord/reply_binding.py` |
| 返信の認可・副作用・送信結果の処理順 | `src/muse_discord/reply_delivery.py` |
| 返信形式の検証・拒否・自発枠と通知の専用照合 | `src/muse_discord/reply_policy.py` |
| サーバー・DMの応答開始条件と共有サーバー判定 | `src/muse_discord/admission.py` |
| 応答回数・メディア回数・対象者のポリシー | `src/muse_discord/response_policy.py` |
| 呼びかけ・会話履歴・会話参加者 | `src/muse_discord/conversation.py` |
| 状態ファイル・未反映キュー項目の保存と再試行 | `src/muse_discord/durable_state.py` |
| 共有キューの読み取り・差分更新 | `src/muse_discord/message_queue.py` |
| outboxの取得・再起動復旧・送信後の確定 | `src/muse_discord/outbox_store.py` |
| 段階間で受け渡すメッセージの型 | `src/muse_discord/message_models.py` |
| 受信添付の選別・収集・期限切れ削除 | `src/muse_discord/media/collector.py` |
| 受信添付の安全な取得・URLと転送先の検査 | `src/muse_discord/media/fetch.py` |
| 文書のテキスト抽出・動画フレーム抽出 | `src/muse_discord/media/documents.py`・`video.py` |
| 生成添付の依頼・チャンネル承認・再試行時の確認 | `src/muse_discord/generated_replies.py` |
| 生成添付のパス検証・圧縮・期限切れ削除 | `src/muse_discord/generated_media.py` |
| ファイル排他・原子的な保存 | `src/muse_discord/storage.py` |
| 送信前の秘密情報検査・ライブnonce収集・所有者への通知制限 | `src/muse_discord/outgoing_guard.py` |
| 送信形式・添付・分割送信・入力中表示 | `src/muse_discord/messaging/` |
| サーバー・チャンネルの探索・所属・設定用スナップショット | `src/muse_discord/directory/` |
| 所有者DMの認可・権限・記憶・通知・承認 | `src/muse_discord/owner/` |
| 通知の権限・登録・発火・復旧・送信先 | `src/muse_discord/watches/` |
| 監視入力の型・検証 | `src/muse_discord/muse_watches.py`（Pydantic） |
| チャンネルの閲覧権限・メンバー取得キャッシュ | `src/muse_discord/channel_access.py` |
| Discordの権限計算 | `src/muse_discord/discord_permissions.py`（discord.py） |
| 設定の型・検証 | `src/muse_discord/settings_schema.py`（Pydantic） |
| HTTPのバックオフ・読み取り期限・認証失敗の記録 | `src/muse_discord/http_runtime.py` |
| モニターの稼働記録・トークン拒否時の終了 | `src/muse_discord/monitor_health.py` |
| Discord HTTP通信 | `src/muse_discord/discord_rest.py`（discord.py） |
| コマンドの定義・差分登録・再試行 | `src/muse_discord/settings_commands.py` |
| ボタン操作の振り分け・匿名応答・専用ワーカー | `src/muse_discord/interaction_dispatch.py` |
| Discord接続・UI操作受付 | `src/muse_discord/discord_connection.py`（discord.py） |
| 設定変更による応答対象・公開範囲・費用の評価 | `src/muse_discord/settings_assessment.py` |
| プリセットの定義・変更案への変換・一致判定 | `src/muse_discord/settings_presets.py` |
| 設定の公開API | `src/muse_discord/muse_settings.py` |
| 設定CLIの引数解析・表示・変更操作・保存済み情報の参照 | `src/muse_discord/settings_cli/` |
| 設定のキー・適用範囲・変更データ型 | `src/muse_discord/settings/values.py`・`places.py`・`changes.py` |
| 設定の読み込み・再検証・継承の解決 | `src/muse_discord/settings/loading.py`・`resolution.py` |
| 設定の変更計画・管理者権限の確認 | `src/muse_discord/settings/planning.py`・`permissions.py` |
| Discord上書きの保存・変更の適用 | `src/muse_discord/settings/overlay.py`・`editing.py` |
| 設定の履歴・取り消し・元ファイルへの反映 | `src/muse_discord/settings/history.py`・`folding.py` |
| 設定の表示文言・名前解決 | `src/muse_discord/settings_display.py` |
| 設定画面の読み書き・ユーザー別セッション | `src/muse_discord/settings_runtime.py` |
| メッセージの処理結果から作る返信診断 | `src/muse_discord/message_outcomes.py` |
| Discord設定画面の公開API | `src/muse_discord/settings_ui.py` |
| 設定画面の下書き・権限・保存と取り消しの判断 | `src/muse_discord/settings_views/model.py` |
| 設定画面のボタン・入力フォーム | `src/muse_discord/settings_views/controls.py` |
| 設定画面の部品配置・表示 | `src/muse_discord/settings_views/panel.py`・`presentation.py` |
| 設定画面のコマンド受付・再描画 | `src/muse_discord/settings_views/commands.py` |
| 返信データの共通検証 | `src/muse_discord/reply_protocol.py` |
| 単一起動ロック・旧PIDの確認 | `src/muse_discord/monitor_process.py` |
| Gateway接続スレッド・キュー・終了状態 | `src/muse_discord/gateway_session.py` |
| プロセス監視 | `bin/watchdog.sh` |
| 通知の公開API | `src/muse_discord/muse_watches.py` |
| 通知依頼とWeb確認結果の形式検証 | `src/muse_discord/watches/schema.py` |
| 通知依頼の認可・追加・取り消し | `src/muse_discord/watches/actions.py` |
| 通知の保存・期限・処理済み記録 | `src/muse_discord/watches/store.py` |
| 通知の文字列照合・公開範囲に応じた表示 | `src/muse_discord/watches/matching.py`・`presentation.py` |
| Web確認の発行・予算・nonce照合 | `src/muse_discord/watches/web_checks.py` |
| 自発発言の実行管理・スロットの発行と復旧 | `src/muse_discord/proactive/runtime.py` |
| 自発枠とワーカー向け文脈の作成 | `src/muse_discord/proactive/prompts.py` |
| 自発枠の照合・送信・再試行 | `src/muse_discord/proactive/delivery.py` |
| 元の発言に基づく解除・再開・時刻設定の確認依頼 | `src/muse_discord/proactive/actions.py` |
| 自発発言の対象・時間帯・上限の判定 | `src/muse_discord/proactive/targets.py` |
| サーバーとDMごとの実行時刻・対象の交替 | `src/muse_discord/proactive/heartbeat.py` |
| 自発発言の保存・破損時の復旧・nonce公開 | `src/muse_discord/proactive/store.py` |
| 本人への承諾確認・再試行・ボタンの照合 | `src/muse_discord/proactive/consent.py` |
| 単発CLI・MCP | `bin/discord-api.py`・`src/muse_discord/discord_tools/` |
| 所有者コマンドの引数・表示・実行先選択 | `src/muse_discord/discord_tools/cli.py` |
| 所有者コマンドと本人DMの認可・対象制限 | `src/muse_discord/discord_tools/authorization.py` |
| 単発ツールのトークン・通信・生成添付の検査 | `src/muse_discord/discord_tools/session.py` |
| 単発ツールの秘密情報検査 | `src/muse_discord/discord_tools/privacy.py` |
| 外部アプリのポリシー・承認・実行 | `src/muse_discord/external_apps/` |

## キューと返信

- モニターが `reply_queue.json` を書く。ワーカーはwakeで渡された `items` と `persona` だけを処理し、キュー全体を読まない。
- ロックを取得できなければ書き込みを延期する。未保存の受信項目は `state.json` の `queue_backlog` に保管し、次の周回で再試行する。送信後のキュー削除とnonce消費も、ロック取得後に行う。
- 送信後の確定を延期するときは、再試行の回数・開始時刻・次回時刻を `state.json` の `outbox_retries` に保存する。自発発言の未登録項目は `proactive.json` のスロットに保存し、期限内に同じIDとnonceで再登録する。
- キュー項目には `message_id`・`channel_id`・`kind`・発言者・本文・履歴・設定を含む。権限はモニターが付与する。
- CLIとモニターは `reply_protocol.py` で返信データの型と送信内容を検証する。nonce・依頼との対応・権限は各呼び出し元でも確認する。
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

設定の階層・上書き判定は `settings/resolution.py`、ファイル解析は `settings/loading.py` に従います。同一階層のDiscord上書きは、元ファイルが変更されると退役します。
サーバー管理者の設定変更は、そのサーバーを静かにする範囲だけです。Bot権限から所有者の権限を付与しません。
`/settings` はdiscord.pyの部品・コールバックを使い、変更を下書きにまとめて保存します。操作のたびに所有者・管理者の権限を再確認し、編集中に対象の設定が変わった場合は上書きしません。
Gatewayの再接続とプレゼンス更新はdiscord.pyに任せ、メッセージは既存の受信キューへ渡します。RESTポーリングによる受信漏れの補完と重複排除は維持します。
モニター・所有者用CLI・導入ツールのDiscord REST通信は `discord_rest.py` に集約します。SDKがHTTP接続・短いレート制限・multipartを扱い、長い制限と呼び出しの時間切れは呼び出し元へ返します。送信前の認可と秘密情報検査は呼び出し元で行います。
メッセージはnonceで再試行時の重複を防ぎます。識別子のない新規作成は、応答が5xxでもSDKに自動再試行させません。SDKの内部HTTPClient・Routeへの依存を通信層内に限定するため、discord.pyは2.7系（2.7.1以上）に制限し、更新時はローカルHTTP試験で確認します。

## 追加機能の契約

- リマインダー・監視は依頼元に通知し、専用nonce・対象・回数・期限を検証する。Web監視は所有者のDMだけ。
- `schedule` とWeb監視の結果報告はPydanticの型で検証する。日時の許容範囲・利用権限・上限・nonceの有効性は実行時にも確認する。
- ロール・メンバー・チャンネルの権限は、取得済み情報からSDKモデルを作り、discord.pyで計算する。取得失敗や不正な権限情報では監視通知を許可しない。非公開スレッドの除外とメンバー取得の期限付きキャッシュはモニターが管理する。
- SDKモデルの構築を `discord_permissions.py` に限定する。対応版はdiscord.py 2.7系（2.7.1以上）。ロール・チャンネル・所有者の変更を計算へ反映し、削除されたロールをキャッシュから権限へ戻さない。
- Webチェッカーは取得内容を実行指示にせず、`bin/watch-report.py` で結果を返す。
- 自発メッセージは初期値が無効。対象・時間・上限・解除を確認し、スロットのnonceを一度だけ使う。
- CLIの既読位置はモニターの配信状態から独立する。[CLI・MCP](TOOLS.md) を参照。
- 外部アプリは `plan`→`execute`。本人DM・現在の権限・5分の期限を確認し、同じ計画を再実行しない。[設定と承認](EXTERNAL_APPS.md) を参照。

## 初期化と処理の境界

`monitor` のimportでは設定やトークンを読み込まず、サービスを生成しない。`main()` が `initialize()` を呼び、テストも設定とトークンを指定して明示的に初期化する。
自発発言は `ProactiveContext` に宣言した操作と状態の参照だけを受け取る。モニターの名前空間全体を渡さない。保存状態と破損時の復旧は `ProactiveStore`、承諾ボタンのロックと公開中のトークンは `ConsentRequests` が持つ。保存状態の読み込み時に承諾ボタンの参照を復元し、クリックは本人・期限・一度だけの使用を確認した後、メインスレッドで再確認して記録する。

`Proactive` は各サービスを接続して実行時刻とスロットの寿命を管理する。
文脈の作成は `SlotPrompts`、照合と送信・再試行は `SlotDelivery`、
元の発言を検証して同意の変更を受け付ける処理は `ConsentActions` が担当する。
受信・返信の各段階は、検証済みの入力と処理結果を値として渡す。前のメソッドによる属性の追加を前提にしない。
受信項目の組み立ては `ingest`、返信の処理順は `reply_delivery`、取得・復旧・確定のトランザクションは `outbox_store` が持つ。これらからモニターをimportしない。機能のテストは担当モジュールを直接使い、モニターにはテスト専用の中継関数を置かない。
応答ポリシーは設定と所有者の取得関数を受け取り、再読み込み後も同じポリシーの呼び出しに反映する。
通知の登録と対象者判定は `watches.requests`・`watches.audience`、発火と復旧は `watches.runtime` が持つ。閲覧権限の確認は保存ロックの前、キュー登録は保存後に行う。
所有者DMの認可は `owner.authorization`、権限発行・再発行は `owner.capabilities`、原文からのコマンド対象解決は `owner.targets`、記憶への保存は `owner.memory` が持つ。権限の消費はoutboxの確定時に行う。
所有者の在籍確認は `owner.presence`、通知は `owner.messaging`・`owner.admin_digest`・`owner.join_notices`・`owner.updates`、返信の保留と承認は `owner.review` が持つ。更新通知も共通の設定Watcherを通す。
サーバーとチャンネルの探索は `directory.discovery`、参加退出は `directory.lifecycle`、設定画面への公開は `directory.snapshot` が持つ。スナップショットはロック内で交換し、読み手にはコピーを返す。
送信形式は `messaging.sending`、分割送信と重複抑止nonceは `messaging.parts`、入力中表示の期限と更新は `messaging.typing` が持つ。分割返信は全パートを検査してから最初の送信を行い、途中失敗では元の例外に送信済みIDを残す。
連投の集約と再試行は `bursts.PendingBursts`、Gatewayの入力順序は `bursts.GatewayActivity` が持つ。未処理の連投と再試行時刻は保存対象の状態に残し、登録途中の例外ではその試行が追加したキュー項目だけを取り消す。
RESTの巡回と定期ジョブの実行順・待ち時間は `rest_scheduler.RestScheduler` が持つ。時計と実行操作を渡し、巡回と保守処理で時間枠を共有する。再探索要求は復旧直後の周回で消費しない。
サーバー・DMへの応答開始条件は `admission.AdmissionPolicy` が持つ。共有サーバーの確認結果は `SharedServers` が期限付きで保持し、所有者の発言による在籍確認は `owner.presence` に反映する。
設定変更の影響は `settings_assessment.assess` が保存前後の実効値を比較して評価する。UIとCLIは同じ評価を使い、設定エンジンからUIや確認処理を呼び出さない。
RESTとGatewayの受信は `receiving.messages`、Gatewayイベントの振り分けは `receiving.gateway` が持つ。`receiving.cursor` はRESTの確認位置とGatewayで処理したIDを分けて保持し、Gatewayの受信で通信断中の未取得メッセージを飛び越さない。既知DMの対応表は `receiving.direct` が持つ。
処理結果と診断理由は `message_ledger.MessageLedger` が保存する。`receiving.reconciliation` は台帳と処理中のIDを `RecoveryScan` にまとめ、確認できないメッセージだけを再登録する。返信済み・保留中・送信待ちの連投は断片からも二重登録しない。
HTTPの長い制限待ちと認証失敗は `http_runtime`、稼働記録は `monitor_health` が持つ。RESTの完了・失敗時に稼働記録を更新し、匿名のinteraction応答が401になってもBotのトークン拒否として扱わない。
送信前の検査は `outgoing_guard.OutgoingGuard` が持つ。値の書き換えは行わず、各文字列と結合した内容を検査する。所有者への通知回数はスレッド間で共有し、ロックを解放してから送ることで再帰した検査でも待ち合わない。
状態ファイルの保存は `durable_state.StateFile`、未反映の依頼を共有キューへ反映する順序は `QueuePublication` が持つ。キュー更新前に復旧可能な状態を保存し、ロック待ちや書き込みの失敗では未反映の依頼を残す。保存済み内容のキャッシュは保存先と組にする。
依頼のメタデータ・時刻・連投全体の完了記録は `reply_state.ReplyState` が持つ。完了対象はその依頼のトリガーと断片だけに限り、別の連投への参照で処理済みにしない。保存ロックと権限の消費は呼び出し元のoutbox確定処理が担う。
プリセットは `settings_presets` が通常の変更案へ変換する。UIとCLIはその変更案を共通の評価・権限確認・保存処理へ渡し、プリセット自体は設定を書き込まない。利用統計へは定義を値として渡し、設定エンジンの内部属性から探さない。
受信内容の検査と隔離は `receiving.quarantine` が持つ。隔離する連投はトリガーと全断片を台帳へ記録し、隔離ファイルが読めない場合は元の内容を `.bad` へ退避してから新しい記録を書く。
生成添付の選択は `generated_replies.GeneratedReplies` が持つ。依頼IDと送信先に結び付いた有効な承認だけを使い、再試行の添付パスも毎回確認する。実際の送信後に承認を消費する処理は `reply_delivery` が担う。
設定画面の実行時アクセスは `settings_runtime.SettingsContext`、ユーザー別の一時保存は `SettingsSessions` が持つ。診断は `message_outcomes.MessageOutcomes` が台帳とディレクトリから組み立て、送信先不明のサーバーメッセージをDMへ分類しない。
コマンドの定義と差分登録は `settings_commands.CommandRegistrar` が持つ。登録状況・再試行時刻・application IDをサービス内で保持し、他のコマンドを消す一括置換は使わない。Gateway側の設定コマンド判定も同じ名前の定義を参照する。
ボタン等の操作は `interaction_dispatch` が受け付ける。振り分け時に現在の所有者を確認し、状態変更は記録キューへ返す。ハンドラーと応答の例外を個別に処理し、後続の操作を止めない。
返信の形式検証・拒否と通知／自発枠の照合は `reply_policy.ReplyPolicy` が持つ。`reply_delivery` は認可・保留・添付・送信・記録のサービスを直接利用し、モニター経由の中継関数に依存しない。通常返信の結び付けは `reply_binding.ReplyBinding` が確認する。開いている依頼・元のチャンネル・その連投の断片・発行済みnonceを照合する。`StalePrompt` はnonce未公開とワーカー指示の不整合を区別し、保留解除と自発枠には既存の専用検証を使う。
`engine` の初期値・型・範囲・補正は `settings_schema.EngineSettings` に集約する。

## 型と静的検査

Pyrightは全体をbasicで検査し、`paths.py`・`secret_scan.py`・`reply_protocol.py` はstrictにする。
型は関数やモジュールの境界から付け、形が決まった辞書には `TypedDict` を使う。外部入力の検証はPydanticが担う。
Discord SDKの型変換は通信・権限・UIの各窓口に限定する。既存の大きなモジュールは、責務を整理しながら段階的に型を付ける。

```bash
uv sync --locked --group apps
uv run --group apps ruff check .
uv run --group apps ruff format --check .
uv run --group apps pyright
```

Ruffは本体の複雑度・引数数も検査する。`tests/` と `e2e/` は期待値の数値と関数の規模・複雑度だけを除外し、構文・バグ検査は維持する。日本語UIの句読点は `allowed-confusables` で明示する。

MCPを含めた型検査にはオプション依存も必要。CIとpre-commitもこの環境で検査する。
実行ファイルとテストには、ソースの探索先を追加した後にimportする箇所がある。そこの `E402` だけを行単位で除外する。

## 検証

```bash
bash tests/run_all.sh
bash tests/run_all.sh onboarding
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

設定形式を変えた場合は設定例とテストを更新し、所有者の `persona.md` は変更しません。
詳細は `bin/release.sh` と `.github/workflows/release.yml` にあります。

## 出典

- 本体：`cszach/muse-discord`、`7bd0706621b791a5adc72a94d06725ed993f406f`。ライセンスは [MIT](../LICENSE)。
- CLI・MCPの参考：`mothue/muse-discord`、`2e7665b02b83be0153e1e2b137192bc4f7c87c82`。[MIT表記](licenses/mothue-MIT.txt)。

設定の再読み込みと所有者一覧は `settings_watch.LiveSettings` が管理する。UIスレッドからの読み込みは即時に最新設定を返し、所有者一覧の更新と変更通知はメインスレッドへ引き渡す。連続した変更は最初と最後の状態にまとめる。エンジン設定は起動時のPydanticモデルを保持し、通常の再読み込みで実行中のサービスを部分的に再設定しない。

設定エンジンの公開窓口は `muse_settings` とし、実装は `settings` パッケージで担当する。キーと適用範囲を土台に、読み込み・継承解決、変更計画・権限確認、保存・履歴の順に依存させる。モジュール間の依存は循環させず、公開窓口には状態や中継関数を置かない。履歴と書き込みは同じoverlayロックを使う。

チャンネル・サーバーと探索メタデータは `DirectoryState` が保持する。`DirectoryRefresh` は探索中に追加されたチャンネルを残し、完了後に同じ辞書へ最新情報を反映する。IDの集合が変わらなくても名前や権限を更新し、既存サービスの参照を古い辞書へ取り残さない。

ワーカーへ渡す参照メッセージと設定カードは `WorkerContext` が作る。受信パイプラインは会話・媒体・通知と、台帳・隔離・依頼記録・権限発行の担当サービスを直接使う。モニターの中継クラスや同じ操作を列挙する専用インターフェースを重ねない。通知のキュー登録も依頼記録と権限発行を直接使い、受信パイプラインへ依存させない。

通知のデータ操作は `watches` 内で形式検証・認可・保存・照合・表示・Web確認に分け、Discord I/Oを持つ実行サービスから利用する。`muse_watches` は状態や中継関数を持たない公開窓口とする。保存は共通のatomic writerを600権限で使い、CLIとモニターの更新は同じ `watches.lock` で直列化する。

単一起動のPIDロックは `MonitorLock` が保持し、取得に失敗した経路では記述子を閉じる。`GatewaySession` はイベントと操作のキュー、接続状態、接続スレッドを持ち、SDKを接続時まで読み込まない。接続スレッドが終了したときは接続状態を落としてメインループを起こし、通常のREST処理へ戻す。

チャンネル権限のキャッシュはプロセス内で一つの `ChannelAccess`、投稿者ロールのキャッシュは `AuthorRoles` が保持する。両者の失敗時の扱いは混ぜない。通知の候補・再試行時刻・Web確認の公開キャッシュは `WatchState` にまとめる。通知ストアは保存先の提供関数を受け取り、保存先が変わると読み込みキャッシュとロック先も切り替える。既存の辞書を参照するサービスがあるため、一覧の更新には `DirectoryState.adopt()` を使う。

設定画面の `Screen` はDiscord SDKから独立し、下書き・同時編集の検知・権限確認・保存と取り消しの判断を持つ。ネイティブ部品はクライアントの `settings_redraw` コールバックへ再描画を依頼し、部品と描画処理を循環参照させない。外部からの受付は応答を保留してからファイル操作を行い、描画前には改めて最新の権限と設定を確認する。

利用統計の中核は標準ライブラリだけで読み込める。設定との接続は `SettingsMetricsSource` の集計と列挙値抽出だけに絞り、設定モジュール全体を渡さない。送信する項目の選別は `usage_stats.payload.build_payload` に集約し、設定・本文・IDなどの生データを追加の送信経路へ渡さない。スナップショットとインストールIDは共通のatomic writerを600権限で使う。
