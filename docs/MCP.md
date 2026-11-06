# MCPから未読取得・返信・メディア取得を使う

`bin/discord-mcp.py` は、単発CLIと同じ機能をstdioのMCPツールとして公開します。
HTTPポートは開きません。ツールごとにCLIを別プロセスで呼び出し、依頼の権限が別の呼び出しへ混ざらないようにしています。

## インストールと起動

MCP部分はPython 3.11以上と、[公式Python SDK](https://github.com/modelcontextprotocol/python-sdk) の2.3系以降を使います。
通常のCLI・モニターの依存には含めていません。

```bash
python3 -m venv .venv-mcp
.venv-mcp/bin/pip install -r requirements-mcp.txt
.venv-mcp/bin/python bin/discord-mcp.py
```

MCPクライアント側には次の起動設定を登録します。実際の絶対パスに置き換えてください。

```json
{
  "mcpServers": {
    "muse-discord": {
      "command": "/path/to/muse-discord/.venv-mcp/bin/python",
      "args": ["/path/to/muse-discord/bin/discord-mcp.py"]
    }
  }
}
```

BotトークンはCLIと同じ `config.yaml` の `token_from` から読みます。
環境変数をトークンの取得元にしている場合は、MCPクライアントが起動する子プロセスにもその環境変数が必要です。
トークンそのものをツール引数や会話に渡す必要はありません。
この実装は接続設定やフック登録を自動で変更しません。

## 公開する10ツール

| ツール | 用途 |
| --- | --- |
| `list_unread` | 指定チャンネルの未読を取得 |
| `mark_read` | 処理済みIDまでローカルの既読位置を進める |
| `get_messages` | 過去の履歴を取得 |
| `send_message` | 長文を分割して送信 |
| `reply_message` | 特定メッセージへ返信 |
| `fetch_attachment` | 指定メッセージの添付ファイルを保存 |
| `get_emoji` | カスタム絵文字・Unicode絵文字の画像を保存 |
| `get_avatar` | 指定ユーザーのアバターを保存 |
| `external_app_plan` | 外部アプリの操作を計画 |
| `external_app_execute` | 許可された計画を一度だけ実行 |

Discord操作の8ツールには `for_id`、`nonce`、`channel_id` が必要です。
`for_id` はモニターが渡した所有者の依頼ID、`nonce` はその依頼の有効な値です。
有効な依頼を持たないクライアントは、ツールを一覧表示できても実行できません。
認可の条件と、未読の初期化・既読操作の意味は [CLIガイド](CLI.md) と共通です。

失敗はMCPの `is_error` として返します。`PARTIAL_SEND` の場合は送信済みIDも含みます。
タイムアウトやキャンセルで中断された場合は、Discord上の送信結果を確認してから再試行してください。

外部アプリの2ツールは、元の本人DMの `for_id` と `owner_dm_nonce` を使います。
Discord操作用のnonceとは認可条件が異なり、`abilities.external_apps` による別の段階制御を受けます。
初期値は無効です。設定・承認結果の形式と実装範囲は [外部アプリ仕様](EXTERNAL_APPS.md) を参照してください。

## 添付・絵文字・アバター

通常のCLIからも同じ機能を使えます。

```bash
python3 bin/discord-api.py --for ITEM_ID --nonce NONCE fetch-attachment CHANNEL_ID MESSAGE_ID ATTACHMENT_ID
python3 bin/discord-api.py --for ITEM_ID --nonce NONCE get-emoji CHANNEL_ID '😀'
python3 bin/discord-api.py --for ITEM_ID --nonce NONCE get-emoji CHANNEL_ID '<a:name:EMOJI_ID>'
python3 bin/discord-api.py --for ITEM_ID --nonce NONCE get-avatar CHANNEL_ID USER_ID
```

添付は、許可されたチャンネルの指定メッセージから取得します。任意のURLを引数として受け付ける機能はありません。
保存先は `media/on-demand/`、上限は1ファイル25MiBです。ファイル名を整形し、権限600で一時保存してから置き換えます。
既存の別ディレクトリへのシンボリックリンクは拒否します。

ダウンロード先はDiscordのCDNと、Unicode絵文字用のTwemoji配信パスに限定します。
HTTPSのみを使い、リダイレクトは拒否します。CDNリクエストにはBotトークンやCookieを付けません。
BotトークンはDiscordのREST API呼び出しだけに使います。

MCPでは、5MiB以下のPNG・JPEG・GIF・WebPをファイル情報と画像ブロックで返します。
それ以外は保存したファイルの情報を返します。取得した内容は外部データとして扱ってください。

## 検証

```bash
python3 tests/discordtools/test_tools.py
python3 tests/discordtools/test_media.py
.venv-mcp/bin/python tests/discordtools/test_mcp.py
```

MCPテストはSDKがない環境ではスキップします。CIではPython 3.13のジョブでSDKを入れて実行します。
実際のMuseへの登録と、Discordへの実送受信・実ファイル取得は別途確認が必要です。
