# 導入・設定

MuseのVMで、本人のDiscord DMだけに応答させる手順です。
初回は「起動確認」まで順番に進め、設定変更は必要なときに参照してください。

## 1. 配置と依存関係

このリポジトリを配置し、以後はそのルートで実行します。

```bash
git clone https://github.com/OrzSabaku/muse-discord ~/muse-discord
cd ~/muse-discord
bash bin/install.sh
```

Python 3.10以上、uv、jqが必要です。`bin/install.sh` はuvで`.venv`を作成します。以後の `python3` は`.venv/bin/python3`に置き換えるか、`uv run`経由で実行します。
既に `config.yaml` がある場合は再導入せず、[更新・復旧](UPDATING.md) を参照してください。

## 2. Botの接続

1. [Discord Developer Portal](https://discord.com/developers/applications) でBotを作成する。
2. 実トークンを権限600のファイルへ安全に配置する。会話・コマンド引数・ログへ貼らない。
3. 次のコマンドを実行し、表示された所有者が本人か確認する。招待・Intent設定は出力の案内に従う。

```bash
python3 bin/connect-discord.py --name "Muse" --token-file TOKEN_FILE_PATH
python3 bin/connect-discord.py --wait 900
```

トークン入力ファイルは読み取り後に削除されます。Portal側の追加操作を案内されたら、対応後に `--saved` で再確認します。
パスワード・2FA・アプリ本体の承認は正規画面で行います。実トークンを安全に渡せない場合は接続を止めます。

## 3. 本人DM用の設定

**起動前に実行します。** 既存設定はバックアップされます。

```bash
python3 bin/personal-profile.py
python3 bin/settings.py validate
python3 bin/render-worker-prompt.py --media none
```

- 所有者を1人に固定し、その人のDMだけを許可する。複数所有者の場合は `--owner USER_ID` を指定する。
- サーバー応答、他のBot、Discord操作コマンド、生成メディア、自発メッセージ、利用統計、更新通知、外部アプリは無効。
- 本人DMの記憶保存は有効。既存の `~/MEMORY.md` に追記し、ファイルがなければ作成しない。
- `config.discord.yaml` があると適用を拒否する。モニター停止後に内容を確認・退避してから進める。

これは初期設定です。後から設定を変更すれば動作も変わります。OSの隔離を提供する機能ではありません。

## 4. Museのフック登録

`<pkg>` を配置先の絶対パスに置き換え、Museの実際のフック機構へ登録します。

| スクリプト | 間隔 | 起動先・指示 |
| --- | --- | --- |
| `hooks/mention-watch.sh` | 5秒 | 専用ワーカー会話で `<pkg>/worker-prompt.md` を読み、wakeの項目だけを処理する |
| `hooks/watchdog-hook.sh` | 10分 | 本体会話で異常を確認し、`<pkg>/docs/UPDATING.md` に従い復旧する |
| `hooks/update-watch.sh` | 60秒 | 更新通知を使う場合。本体会話で指定版へ更新し、終了・中断時に `update_request.json` を削除する |
| `hooks/watch-due.sh` | 60秒 | Web監視を使う場合。別の専用チェッカー会話でwakeの指示だけを処理する |

スクリプトを別の場所へコピーする環境では `MUSE_DISCORD_DIR` を指定します。
チェッカーのツールを制限できる場合は、検索・取得と `bin/watch-report.py` だけを許可します。
フックを登録できない環境では、モニターだけを動かしても返答しません。

## 5. 起動確認

```bash
bash bin/watchdog.sh
python3 bin/doctor.py --no-dm
```

本人のDMへの応答、別アカウントとサーバーへの無応答、再起動後の応答を確認します。
時間のかかる依頼は、追加依頼との順序・重複も確認してください。診断のサーバー応答関連の指摘は本人DM用設定と区別します。
Museの記憶・接続済みサービス・承認画面の動作も導入先で確認します。機密内容をDMへ返すとDiscord側にも保存されます。

## 設定変更

設定の見本は [config.example.yaml](../config.example.yaml)。ファイル編集後は `bin/settings.py validate` で確認します。
Discordの `/settings` や設定CLIによる変更は `config.discord.yaml` に保存され、同じ箇所のファイル変更が優先します。
旧形式の自動変換はありません。整数は数値、無制限は `null`、ユーザーIDは引用符で囲んだ文字列のリストで指定します。
DMの会話による設定変更はできません。Discordの `/settings`、設定CLI、YAMLファイルのいずれかを使います。

```bash
python3 bin/settings.py show --dms --explain
python3 bin/settings.py places
python3 bin/settings.py set style.length brief --place dms --by muse.ai
python3 bin/settings.py undo --place dms --by muse.ai
```

変更先は `defaults`・`dms`・`server:ID`・`channel:SERVER_ID:CHANNEL_ID` です。
終了コード3の変更は未適用です。警告を確認し、その変更への明示的な承認後だけ `--yes` で再実行します。
Discordでは `/settings` を開き、「適用先」→「分類」→「設定項目」の順に選びます。各項目で現在値・設定元・継承する値を確認できます。

- 数値や文章は「値を入力…」から直接入力します。任意の日次上限を指定でき、無制限にできる項目は空欄で指定します。
- 変更はすべて下書きです。「保存」でまとめて適用し、「下書きを破棄」で取り消します。適用先を変える前に保存か破棄を選びます。
- 「上の設定を継承」で個別設定を解除します。「直前の変更を取り消す」は、保存済みの変更を戻します。
- 画面は本人だけに表示され、最後の更新から15分で期限が切れます。モニターの再起動後は `/settings` を開き直してください。

「利用者・権限」で応答対象や所有者コマンドの許可を変更できます。外部アプリ権限は、適用先を「全体の初期設定」にして変更します。[外部アプリ設定](EXTERNAL_APPS.md) を参照してください。
「返信しなかった理由」は当時の処理記録を表示します。設定の修正は編集画面で行い、過去のメッセージへの自動返信はしません。

オンライン表示とステータスはモニターの接続から更新します。`presence.status` が空なら文章を表示せず、`presence.enabled: false` ならオフライン表示にします。どちらもメッセージの受信・返信は続きます。設定変更は接続し直さずに反映し、短時間の連続変更は最後の値にまとめます。

### サーバーとチャンネルの発言設定

[server.example.yaml](../server.example.yaml) を、`config.yaml` と同じディレクトリの `servers/<サーバーID>.yaml` にコピーして編集します。ファイル内には、そのサーバーの設定を直接書きます。`servers:` で囲む必要はありません。同じサーバーを `config.yaml` と別ファイルの両方に定義すると、検証エラーになります。

| 設定 | 書く場所・動作 |
| --- | --- |
| `everywhere.proactive.per_day: 30` | `config.yaml`。全サーバーとDMを合わせた、直近24時間の自発発言数の上限。数値で指定し、`0` は自発発言を停止する。通常の返信数は含めない |
| `proactive.frequency: often` | サーバー別ファイル。`rarely`・`sometimes`・`often` から指定し、サーバーごとに独立した間隔で自発発言を検討する。DMは `config.yaml` の `dms.proactive.frequency` に指定する |
| `channels.<チャンネルID>.replies: freely` | サーバー別ファイル。呼びかけがなくても人の会話に参加できる。すべての発言に返信する義務はない |
| `channels.<チャンネルID>.proactive.enabled: on` | サーバー別ファイル。自発発言を許可する。`freely` と併用すると、会話への参加と自発発言の両方を許可できる |
| `replies: when-addressed` / `replies: off` | 各チャンネルで、呼びかけへの返信だけを許可する／返信を停止する。自発発言も止める場合は `proactive.enabled: off` を併記する |

ファイル編集後は `python3 bin/settings.py validate` で確認します。モニターはファイルの追加・変更・削除を自動で読み直し、不正な編集の場合は直前の有効な設定を使い続けます。全体の日次上限はファイル編集専用です。所有者の在籍確認、ユーザーの拒否設定、静穏時間、未応答時の再送防止は引き続き適用されます。
