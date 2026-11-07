# DiscordでのE2E

専用の非公開サーバーで、Botの受信・返信・再起動を確認します。シナリオごとにチャンネルを作成・削除します。
通常の回帰検証は [オフラインテスト](../tests/README.md) を使ってください。

## 2つの試験

| 段階 | ワーカー・判定 | CIでの扱い |
| --- | --- | --- |
| Tier 1 | 台本付きの偽ワーカー・コードによる判定 | 同一リポジトリのPRで実行し、失敗はチェック失敗 |
| Tier 2 | Claude Codeのワーカー・別実行のjudge | `e2e-full` ラベルで実行。警告扱いで、サブスクリプション利用量を消費 |

forkや認証情報がない環境はスキップします。ローカル実行とCIで同じBotトークンを同時使用しないでください。

## 準備

1. テスト専用サーバーと、対象Bot・driver A・driver Bの3つのBotを作成する。
2. すべてのBotでMessage Content Intentを有効にし、専用サーバーへ招待する。
3. 次の値をローカル環境またはActions secretsへ設定する。実運用のBot・トークンを流用しない。

| 変数 | 値 |
| --- | --- |
| `E2E_GUILD_ID` | 専用サーバーID |
| `E2E_SUT_TOKEN` | 対象Botのトークン |
| `E2E_DRIVER_A_TOKEN`・`E2E_DRIVER_B_TOKEN` | driverのトークン |
| `CLAUDE_CODE_OAUTH_TOKEN` | Tier 2のみ。ログイン済みの環境では `claude setup-token` で取得 |

対象Botの招待権限は `2252074691710016`、driver Aは `309237746768`、driver Bは `274878008384` です。
Aはチャンネル作成・削除とスレッド作成も行います。Server Members Intentは不要です。
`testing.treat_as_human` はdriverを人として扱うテスト専用設定なので、実運用へ持ち込みません。

## 実行

```bash
uv sync
uv run python -m unittest discover -s e2e/tests -t .
uv run python e2e/run.py --tree . --tier 1 --dry-run
uv run python e2e/run.py --tree . --tier 1
uv run python e2e/run.py --tree . --tier 1 --scenario mention_burst --keep-channels
```

Tier 2は `--tier 2` を指定し、ログイン済みの `claude` が必要です。`CLAUDE_BIN` と `E2E_CLAUDE_MODEL` で実行先・モデルを指定できます。
ハーネスはコピーしたツリーのモニターを直接起動し、watchdog・受信フックは動かしません。
結果は `out/results.json`・`summary.md`・`transcripts/`、Tier 2の実行ログは `claude_worker.log`・`claude_wakes/` にあります。

## シナリオの追加

`scenarios/<id>.yaml` を既存例から作り、型と制約は `scenario.py` を参照します。
操作は `send`・`wait_for`・`action`・`converse`・`delete`・`thread`・`settings` などです。
Tier 1の応答指定は `fake_worker.py`、判定項目は `checks.py`、Tier 2の基準は `judge.rubric` にあります。

- 実Discordでしか分からない動作を対象にし、同じ準備を使う既存シナリオへ検証を追加する。
- 固定待機より `wait_for` を使う。無応答を検証するときは、続けて応答できることも確かめる。
- 追加後は `--dry-run` とハーネスのユニットテストを実行する。

## 実アカウントで確認する項目

Bot同士のDMや、他のBotのスラッシュコマンド・ボタン操作には制約があるため、次は本人のアカウントで確認します。

- `/settings` の設定変更・確認・Undo、会話からの設定変更と権限昇格の拒否。
- `/settings` の下書きが保存前に適用されず、破棄できること。適用先・分類・設定項目の選択、数値の直接入力、継承表示、期限切れ後の開き直し。管理者・一般メンバーの操作範囲。
- DMとサーバー投稿を同時に送り、実行とnonceが混在しないこと。
- 自発メッセージ・Web監視・外部アプリと正規承認画面の接続。
