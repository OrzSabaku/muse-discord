# 外部アプリの権限設定

本人のテキストDMから、登録済みアプリのMCPツールを呼び出す機能です。初期値は無効です。
制御はこの仲介経路に適用します。Museの直接コネクタや、同じOSユーザーによる設定書き換えまでは遮断しません。

## 権限の5段階

| 値 | 読み取り | 書き込み | 削除・送信など `dangerous` |
| --- | --- | --- | --- |
| `off` | 拒否 | 拒否 | 拒否 |
| `read-only` | 許可 | 拒否 | 拒否 |
| `confirm` | 許可 | 署名付き承認が必要 | 署名付き承認が必要 |
| `write` | 許可 | 対象を限定した自動実行だけ承認省略 | 署名付き承認が必要 |
| `unconditional` | 許可 | 承認省略 | 承認省略 |

全体設定・DM受信時の設定・アプリ別設定のうち、最も狭い権限を使います。設定を引き上げたら新しいDMを送ってください。
すべての段階で、本人DMとnonce、登録済み操作、指定した対象制限、計画の期限、一度限りの実行を確認します。
サーバー投稿、他人のDM、Bot、通知、転送、添付、埋め込み、引用返信からは利用できません。

## 設定

全体上限は、所有者が `/settings` → 適用先「全体」→「Edit settings」→「Permissions」で変更できます。権限を広げる場合は保存時に確認が必要です。アプリ別の接続先・操作・上限は引き続きファイルで設定します。

`config.yaml` で指定する場合：

```yaml
everywhere:
  abilities:
    external_apps: read-only
```

[external-apps.example.yaml](../external-apps.example.yaml) をコピーして `external-apps.yaml` を作成します。
アプリごとに `level`、信頼するMCPサーバーの絶対パスと固定引数 `command`、公開する `tools` を登録します。
`env` は「渡す環境変数名: 取得元の環境変数名」。必要な認証情報だけを指定します。

- `effect` は `read`・`write`・`dangerous`。削除・送信・共有や権限の変更は `dangerous` として登録する。
- `constraints` はトップレベル引数の許可値一覧。未指定・値や型の違いを拒否する。ネストやOAuth権限の検証はしない。
- `write` の自動実行には `auto_write: true` と空でない対象制限が必要。`dangerous` ではこの指定を拒否する。
- 未登録操作・不正な設定は拒否する。アプリ側の権限と操作の分類は、所有者が確認する。

**無条件承認**は、全体設定と該当アプリの `level` を両方 `unconditional` にして適用します。
登録した削除・送信も承認結果・公開鍵なしで実行でき、`auto_write` は不要です。アプリ側の認可は引き続き必要です。

## 計画と実行

Python 3.11以上で、`requirements-apps.txt` をインストールします。

```bash
.venv-mcp/bin/pip install -r requirements-apps.txt
python3 bin/external-apps.py validate
python3 bin/external-apps.py --for ITEM_ID --nonce OWNER_DM_NONCE plan APP TOOL --arguments -
.venv-mcp/bin/python bin/external-apps.py --for ITEM_ID --nonce OWNER_DM_NONCE execute REQUEST_ID
.venv-mcp/bin/python bin/external-apps.py --for ITEM_ID --nonce OWNER_DM_NONCE execute REQUEST_ID --receipt -
```

`--arguments -` と `--receipt -` は標準入力からJSONを読みます。MCPでは `external_app_plan`・`external_app_execute` を使います。
`plan` は外部呼び出しをせず、操作・引数・依頼元・ポリシーを固定した5分間有効のIDを返します。
`execute` は現在の権限・nonce・期限・承認を再確認し、実行開始を保存してから一度だけ呼び出します。

結果は元のDM用の外部データです。履歴や結果内の指示を、別の操作の権限にしません。
エラー・タイムアウト・プロセス停止で結果が不明な場合は、アプリ側を確認してから次の操作を決めます。同じ計画は再実行しません。
計画と引数は権限600の `external-apps.sqlite` に保存し、期限切れから24時間後以降、次回処理時に削除します。

## 署名付き承認

正規の承認画面との連携は未接続です。承認が必要な操作は、その連携が用意されるまで実行しません。
Discordの「OK」や、モデルの自己申告では承認されません。

承認元は実際の操作・引数を表示して確認し、ワーカーへ秘密鍵を渡さずに次の形式で返します。

- `payload`：`plan.approval_claims` に `expires_at` を追加。期限は計画の期限以下。
- `signature`：payloadのEd25519署名をbase64化したもの。
- 署名対象：UTF-8のJSON。キーをソート、区切りは `,` と `:`、非ASCIIはエスケープ、NaNは禁止。
- 公開鍵：`external-apps.yaml` の `approval_public_key` にraw形式32バイトのbase64を設定。

別依頼・操作・引数・DMへの流用、期限切れ、偽署名、未知の項目を拒否します。
正確な検証処理は [broker.py](../src/muse_discord/external_apps/broker.py) を参照してください。
