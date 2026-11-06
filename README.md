# Muse Discord（本人DM用）

自分のMuseをDiscordのDMから使うための非公式配布物です。
MuseのVMに配置し、受信フックでワーカーを起動します。

## 使い方

- [導入・設定](docs/SETUP.md)：最初に読む手順。
- [CLI・MCP](docs/TOOLS.md)：未読取得、返信、メディア取得。
- [外部アプリ](docs/EXTERNAL_APPS.md)：接続先と承認段階の設定。
- [更新・復旧](docs/UPDATING.md)：更新、停止、問題の切り分け。
- [開発仕様](docs/PROTOCOL.md)：構成、ファイル契約、テスト、リリース。

本人DM用の初期設定は、所有者1人だけに応答します。
サーバー応答・利用統計・更新通知・外部アプリは無効です。
DiscordやMuseでの実動作は、導入先で確認してください。

## 主な配置

`bin/` は操作用コマンド、`hooks/` は受信・監視、`prompts/` は実行用指示です。
Python本体と実行状態はルートに置き、`discord_tools/` と `external_apps/` に追加機能を分けています。
`config.yaml`・生成物・実行状態はGit管理から除外しています。

ライセンスは [MIT](LICENSE)。上流の履歴は [CHANGELOG.md](CHANGELOG.md) を参照してください。
