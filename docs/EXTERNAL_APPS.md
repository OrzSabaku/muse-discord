# Discordからの外部アプリ利用を段階的に制御する

この仕様は、Discordの本人DMを起点に、登録済みの外部アプリへアクセスする際の権限を定めます。
CLI・MCPの共通処理で、依頼元・権限・承認・有効期限を検証してから、登録アプリのMCPツールを呼び出します。
初期値は無効です。本人DM用プロファイルも無効のままです。

仲介処理、署名付き承認の検証、MCP接続は実装済みです。Muse本体の承認画面から承認結果を発行する連携は未接続です。
承認が必要な操作は、その連携と公開鍵が用意されるまで実行しません。

## 1. 権限の5段階

| 設定値 | 読み取り | 書き込み | 削除・送信・権限変更など |
| --- | --- | --- | --- |
| `off` | 拒否 | 拒否 | 拒否 |
| `read-only` | 登録した操作だけ許可 | 拒否 | 拒否 |
| `confirm` | 登録した操作だけ許可 | 操作ごとに署名付き承認が必要 | 操作ごとに署名付き承認が必要 |
| `write` | 登録した操作だけ許可 | 対象を限定し、自動実行を明示した操作だけ承認を省略できる | 操作ごとに署名付き承認が必要 |
| `unconditional` | 登録した操作だけ許可 | 署名付き承認を省略 | 署名付き承認を省略 |

全体設定、DMがキューに入った時点の設定、アプリ別設定のうち、最も制限の強い段階を適用します。
設定を後から引き上げても、古いDMの権限は広がりません。引き下げは実行前の再確認で反映します。

操作の種類は、所有者が登録時に `read`・`write`・`dangerous` のいずれかに分類します。
ツール名やアプリが返す説明文から、モデルが権限を推測・追加することはありません。
削除、他人への送信、共有設定や権限の変更は `dangerous` として登録します。
実際の副作用を仲介処理が自動判定する機能はないため、信頼できるアプリを登録し、アプリ側の認可範囲も合わせて設定します。

## 2. 認可する依頼元

対象は、`config.yaml` の `owners` に含まれる利用者本人からの、新しいテキストDMです。
サーバー投稿、他人のDM、Bot、リマインダー、自発メッセージ、転送、添付、埋め込み、引用返信からは利用できません。
バーストの一部にそれらが含まれる場合も拒否します。

モニターは、キュー項目の `external_apps` に `eligible` とキュー投入時の段階を記録します。
既存の `owner_dm_nonce` を本人DMだけの実行に渡し、CLI・MCPはその値と未処理キューを照合します。
`abilities.owner_commands` はDiscord自身の操作権限なので、外部アプリの権限とは独立です。

履歴・返信元・取得した文書・アプリの返答は参照データです。それらを、接続許可、承認、設定変更の根拠にしません。

## 3. 全体設定とアプリ別設定

`config.yaml` に全体の上限を設定します。既定値は `off` です。

```yaml
version: 1
everywhere:
  abilities:
    external_apps: read-only
```

この項目は全体設定だけに置けます。Discordの設定画面や自然言語の設定変更からは編集できません。
権限を変える場合は、信頼できる管理経路でファイルを編集します。

`external-apps.example.yaml` を参考に、ルートに `external-apps.yaml` を用意します。
このファイルはGitの管理対象から除外しています。

```yaml
version: 1
apps:
  calendar:
    level: read-only
    command: [/absolute/path/to/calendar-mcp-server]
    env:
      CALENDAR_ACCESS_TOKEN: MY_CALENDAR_ACCESS_TOKEN
    tools:
      list_events:
        effect: read
        constraints:
          calendar_id: [personal]
      create_draft:
        effect: write
        auto_write: true
        constraints:
          calendar_id: [personal]
      send_invitation:
        effect: dangerous
        constraints:
          calendar_id: [personal]
```

`command` には信頼するMCP stdioサーバーの実行ファイルと固定引数を登録します。先頭は絶対パスとし、シェル展開は行いません。
上のアプリ名・ツール名・引数は説明用です。実際に接続するサーバーの定義に合わせてください。

`env` は「アプリに渡す環境変数名: 取得元の環境変数名」です。必要な認証情報だけを明示します。
任意の環境変数をまとめて継承せず、SDKの基本的なOS用環境変数に、この対応表だけを加えます。

`constraints` はツールのトップレベル引数を値の一覧で制限します。指定した引数が欠けている場合、値や型が違う場合は拒否します。
ネストした引数や、アプリ側のアカウント・OAuth権限の範囲を検証する機能はありません。

`write` 段階で承認を省略できるのは、操作が `write`、`auto_write: true`、空でない対象制限がある場合だけです。
`dangerous` での自動実行指定と、対象制限のない自動書き込み設定は、設定ファイルの検証時に拒否します。
未登録アプリ、未登録ツール、不正な設定は拒否し、以前の許可状態へ戻して実行することもありません。

### 無条件承認

全体設定とアプリ別設定をともに `unconditional` にした後、新しい本人DMを送ると適用されます。
登録済みの読み取り・書き込み・削除・送信などは、署名付き承認結果や公開鍵を使わずに実行できます。
`auto_write` の指定は不要です。アプリ側の認可は引き続き必要です。

```yaml
# config.yaml
everywhere:
  abilities:
    external_apps: unconditional
```

```yaml
# external-apps.yaml の該当アプリ
apps:
  calendar:
    level: unconditional
    # command、env、tools は登録済みの設定を維持
```

本人DM・nonceの確認、登録済みアプリと操作、指定した対象制限、計画の期限、一度限りの実行は維持します。
一方が `write` や `confirm` の場合や、DM受信時の段階がそれらの場合は、その段階の承認規則を適用します。

## 4. 計画してから一度だけ実行する

呼び出しは `plan` と `execute` に分けます。`plan` は外部アプリを呼びません。

1. 本人DM、現在のnonce、許可するアプリ・操作・対象を確認する。
2. 操作と引数を保存し、5分間だけ有効なランダムIDを返す。
3. 承認が必要なら、正規の承認画面で実際の操作と引数を確認し、署名付き承認結果を受け取る。
4. `execute` で本人DM・nonce・現在の設定・有効期限・承認結果を再確認する。
5. 実行開始を記録してから外部アプリを呼ぶ。同じIDは再実行しない。

計画にはアプリ、ツール、引数、依頼ID、所有者、DMチャンネル、nonceのハッシュ、実効ポリシーのハッシュを結び付けます。
実行時に引数を差し替える引数はありません。元の依頼が閉じた場合、所有者から外れた場合、nonceや実効ポリシーが変わった場合も拒否します。

アプリのエラー、タイムアウト、結果不明、実行途中のプロセス停止でも、自動再実行しません。
状態が `running`・`unknown` の計画については、アプリ側の結果を確認してから次の操作を決めます。
この仕組みが保証するのは「同じ計画で呼び出しを繰り返さないこと」であり、外部サービスでの処理完了を保証するものではありません。

## 5. 署名付き承認結果

承認元には、Discordワーカーへ秘密鍵を渡さない、独立した信頼できる仕組みが必要です。
Museの正規承認画面との連携では、表示する内容を実際に署名する操作・引数から作成し、その確認後にだけ署名します。
モデルが書いた要約や、Discordの「OK」、ボタンを押したという自己申告を承認結果にしません。

`external-apps.yaml` の `approval_public_key` に、承認元のEd25519公開鍵をraw形式32バイトのbase64で登録します。
検証する承認結果は次の形式です。

```json
{
  "payload": {
    "request_id": "planが返した値",
    "for_id": "元の依頼ID",
    "owner_id": "所有者ID",
    "channel_id": "元のDMチャンネルID",
    "app": "登録アプリ名",
    "tool": "登録ツール名",
    "arguments": {"calendar_id": "personal"},
    "effect": "write",
    "operation_digest": "planが返した値",
    "policy_digest": "planが返した値",
    "plan_expires_at": 1234567890.0,
    "expires_at": 1234567880.0
  },
  "signature": "payloadへのEd25519署名のbase64"
}
```

`payload` は `plan` の `approval_claims` をそのまま使い、`expires_at` だけを追加します。
署名対象はUTF-8のJSONです。キーをソートし、区切りは `,` と `:`、非ASCII文字はエスケープし、NaNは許可しません。
実装の `external_apps.policy.canonical` と同じ形式です。

承認結果の有効期限は計画の期限以下に限定します。別の依頼・DM・所有者・操作・引数への流用や、未知の項目を含む結果は拒否します。
公開鍵、署名、検証ライブラリ、承認元との連携が用意できない場合は、承認が必要な処理を保留します。
承認元の実装・鍵管理・Museの承認画面との接続は、このリポジトリではまだ提供していません。

## 6. CLIとMCP

実行と承認検証には、Python 3.11以上でオプションの依存をインストールします。

```bash
.venv-mcp/bin/pip install -r requirements-apps.txt
python3 bin/external-apps.py validate
python3 bin/external-apps.py --for ITEM_ID --nonce OWNER_DM_NONCE plan calendar list_events --arguments '{"calendar_id":"personal"}'
.venv-mcp/bin/python bin/external-apps.py --for ITEM_ID --nonce OWNER_DM_NONCE execute REQUEST_ID
.venv-mcp/bin/python bin/external-apps.py --for ITEM_ID --nonce OWNER_DM_NONCE execute REQUEST_ID --receipt - < approval.json
```

大きい引数や機密内容を含む引数は、`plan ... --arguments -` で標準入力から渡します。
MCPは既存の `bin/discord-mcp.py` に、`external_app_plan` と `external_app_execute` を追加しています。
外部アプリの結果は `untrusted: true`、返答先は元のDMの `channel_id` として返します。ワーカーはその依頼への返答にだけ使います。

計画と状態は権限600の `external-apps.sqlite` に保存します。引数も含むため、機密データとして扱います。
期限切れから24時間以上たった行は、次回の計画・実行処理時に削除します。

## 7. 制御の対象と制限

この実装が強制するのは、上記の仲介経路を通る操作です。
Museが元から公開している別のアプリツール、ブラウザ、シェルからの直接操作や、同じOSユーザーによる設定ファイルの書き換えまでは遮断しません。
登録アプリのサーバープロセスも、同じOSユーザーの権限で動く信頼対象です。

Discord起点の外部操作全体をこの仕様で制限するには、実行環境側でワーカーの直接コネクタと設定書き換え権限を外し、仲介経路だけを公開する必要があります。
その環境設定とMuseの承認画面への接続は未検証です。実アカウントで利用を広げる前に、接続先と承認元を確認します。

## 8. 検証項目

- 5段階の全体設定とアプリ別設定を組み合わせ、最も狭い権限が適用される。
- 本人DM以外、転送・添付・引用返信、偽nonce、依頼終了後の実行を拒否する。
- 未登録操作、対象の違い、引数の型の違い、設定変更後・期限切れの実行を拒否する。
- 偽署名、期限切れ承認、別操作・引数・DMへの流用を拒否する。
- `unconditional` は署名なしで書き込み・削除を実行でき、より低い全体・アプリ・DM受信時の段階は超えない。
- 同時実行・再起動・結果不明でも、同じ計画で外部呼び出しを繰り返さない。
- 偽MCPアプリとの実通信で、登録した認証情報だけを渡し、Botトークンや無関係な環境変数を渡さない。
- 既存のDiscord返信・設定・監視機能を維持する。
