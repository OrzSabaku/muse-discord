# 利用統計の受信Worker

`src/muse_discord/telemetry.py` が送る匿名の日次報告をCloudflare WorkerとD1で受信します。デプロイは保守担当者だけが行います。

| API | 用途 |
| --- | --- |
| `POST /v1/report` | 4KB以下の既知フィールドだけを検証し、`install_id`・UTC日付で保存。同日再送は置換、1日10回まで |
| `GET /v1/stats` | 直近7日・30日の稼働数、サーバー数、返信数。1時間キャッシュ |
| `GET /v1/badge/servers` | shields.io用のサーバー数 |

IP・ヘッダー・受信時刻は保存しません。`wrangler.toml` のリクエストログ・traceも無効にしています。

## デプロイ

Node 18以上とCloudflareアカウントを使い、このディレクトリで実行します。

```bash
npx wrangler login
npx wrangler d1 create muse-telemetry
# 出力のdatabase_idをwrangler.tomlへ設定
npx wrangler d1 execute muse-telemetry --remote --file=schema.sql
npx wrangler deploy
```

URLを `src/muse_discord/telemetry.py` の `TELEMETRY_URL` に設定します。パス・末尾スラッシュは付けません。
確認先は `<Worker URL>/v1/stats` です。

## 変更・調査

```bash
node --test
npx wrangler d1 execute muse-telemetry --remote --command "<SQL>"
```

報告フィールドを変える場合は、`src/muse_discord/telemetry.py` と `src/validate.js` の `SCHEMA` を揃え、`schema.sql` と既存DBの列も更新します。
古いDBに `ahead`・`modified` 列がない場合は、それぞれ `INTEGER NOT NULL DEFAULT 0` で追加します。
送信側との互換性はリポジトリの `tests/telemetry` でも確認します。
特定のインストールを削除する場合は、`bin/telemetry.py status` で確認したIDを対象にします。
