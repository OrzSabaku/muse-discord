# Muse Discord（本人DM用）

自分のMuseをDiscordのDMから使うための非公式配布物です。
`cszach/muse-discord` のコミット `7bd0706621b791a5adc72a94d06725ed993f406f` を元に、
本人のDMだけを受け付ける設定ツールを追加しています。

初めて導入するときは [日本語の導入手順](docs/START_HERE_JA.md) を読んでください。
MuseのVMに配置し、受信フックを登録して動かします。

## 文書

| 目的 | 読むもの |
| --- | --- |
| 本人DM用に導入する | [日本語の導入手順](docs/START_HERE_JA.md) |
| 依存関係・Bot接続・フックを設定する | [セットアップ](docs/SETUP.md) |
| 単発CLIで未読取得・既読管理・返信する | [CLIガイド](docs/CLI.md) |
| MCPから返信・メディア取得を使う | [MCPガイド](docs/MCP.md) |
| 設定を変更する | [設定ガイド](docs/SETTINGS.md) |
| 問題を調べる | [トラブルシューティング](docs/TROUBLESHOOTING.md) |
| 手動で更新する | [更新手順](docs/UPDATING.md) |
| 実装やワーカーの契約を調べる | [プロトコル仕様](docs/PROTOCOL.md) |
| リリースを作る | [リリース手順](docs/RELEASING.md) |
| 上流の変更履歴・配布時の検証記録を読む | [変更履歴](CHANGELOG.md) / [検証記録](docs/VALIDATION.txt) |

## 構成

```text
docs/                   導入・運用・仕様の文書
prompts/                ワーカーとWeb監視用のプロンプト原本
bin/                    導入・診断・設定などのコマンド
discord_tools/          CLI・MCP共通の未読管理・返信・メディア取得
hooks/                  Museの受信・監視フック
plugins/                拡張機能
tests/                  オフラインテスト
e2e/                    Discordを使うE2Eと、そのテスト基盤
telemetry/worker/       利用統計の受信サービス
monitor.py              Discordの受信・返信処理
presence.py             Botのプレゼンス管理
muse_settings.py        設定の読み込み・変更
settings_ui.py          Discordの設定画面
settings_nl.py          自然言語による設定変更
muse_watches.py         リマインダー・監視
proactive.py            自発的なメッセージ
secret_scan.py          秘密情報の検出
telemetry.py            利用統計の送信
watchdog.sh             プロセスの監視・再起動
config.example.yaml     設定の見本
```

Python本体は、同じディレクトリからモジュールを読み込み、設定やキューを扱う設計です。
`config.yaml`、生成した `worker-prompt.md`、実行中の状態ファイルもルートに置き、Gitでは除外しています。
返信ワーカー用プロンプトの変更後は `bin/render-worker-prompt.py` で再生成します。

## 開発時の確認

リポジトリのルートで実行します。

```bash
python3 -m unittest discover -s tests -p test_personal_profile.py
bash tests/run_all.sh
python3 -m unittest discover -s e2e/tests -t .
node --test telemetry/worker/test/worker.test.js
```

詳しくは [オフラインテスト](tests/README.md) と [E2E](e2e/README.md) を参照してください。
導入テストでは専用の `persona.md` フィクスチャを使います。
E2Eは `e2e/fixtures/test.png` が不足しているため、一部の既存テストが失敗します。
Discord・Museのフック・接続済みサービスでの動作は未確認です。

ライセンスは [MIT](LICENSE) です。
