# 更新・復旧

異常通知では「困ったとき」から確認し、更新は所有者の依頼がある場合に進めます。
既存の設定・トークン・persona・プラグイン・実行状態を保ったまま更新します。
再セットアップは不要です。以下はリポジトリのルートで実行します。

## 1. 更新対象の確認

更新通知がある場合は、その `version` を使います。手動で最新リリースを探す場合は次を実行します。

```bash
git ls-remote --tags --refs origin 'v*' | sed 's#.*refs/tags/##' \
  | grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
  | sort -V | tail -1
```

何も表示されなければリリースはありません。次の `TAG` を選んだ版に置き換え、変更履歴とローカル変更を確認します。
`git merge-base --is-ancestor "$TAG" HEAD` が成功する版は導入済みです。

```bash
TAG=vX.Y.Z
git fetch --no-tags origin tag "$TAG"
OLD=$(git rev-parse HEAD)
git describe --tags --match 'v[0-9]*' --abbrev=0 2>/dev/null \
  || echo unreleased
git log --oneline HEAD.."$TAG"
git diff HEAD "$TAG" -- CHANGELOG.md
git status --short --untracked-files=no
git log --oneline "$TAG"..HEAD
```

## 2. コードの更新

ローカル変更・独自コミットがない場合：

```bash
git merge --ff-only "$TAG"
```

変更がある場合は、差分と競合するファイルを確認し、所有者と扱いを決めてからバックアップします。
未コミットの変更があるときだけstashを使い、独自コミットがある場合は通常のmergeで取り込みます。

```bash
git diff --stat
git diff --stat HEAD "$TAG"
git branch "backup/pre-update-$(date +%Y%m%d-%H%M)"
git diff > "$HOME/muse-discord-local-changes-$(date +%Y%m%d-%H%M).patch"
git stash push -m "local changes before update"
git merge --no-edit "$TAG"
git stash pop
```

競合したら停止し、ファイルごとに解消します。変更を捨てる `reset --hard`・`clean`・`stash drop` は使いません。
`main` を追う場合は `git fetch` 後、変更なしなら `git pull --ff-only`、独自コミットありなら `git pull --no-rebase` を使います。

## 3. 設定・指示・フックの更新

```bash
bash bin/install.sh
uv run python bin/settings.py validate
uv run python bin/render-worker-prompt.py --media none
```

生成メディアを使っている場合は、`--media` を従来の値に合わせます。個別に変更したseamも反映します。
`CHANGELOG.md` の **Action needed**、`config.example.yaml` の差分、[フック登録](SETUP.md#4-museのフック登録) を確認します。
フックをコピーして登録している場合はコピーも更新します。MCP・外部アプリの依存は [CLI・MCP](TOOLS.md) を参照してください。
設定は現行形式のみ対応します。`config.example.yaml` に合わせて編集し、`validate` で確認します。

起動コマンドは `bash bin/watchdog.sh` です。cronなどに旧パスを登録している場合は更新します。

## 4. 再起動と確認

```bash
kill "$(cat monitor.pid)"
bash bin/watchdog.sh
uv run python bin/doctor.py --no-dm
```

旧版から初めて更新する場合は、残っている旧 `presence.py` プロセスも停止します（`pkill -f "^python3 (bin/)?presence.py"`）。現在はモニターがオンライン表示とステータスを担当します。

本人DMへの返答を確認し、稼働版と未対応事項を記録します。更新要求がある場合は、完了・中断時に `update_request.json` を削除します。
戻す場合は、所有者と確認したうえで `git reset --keep "$OLD"` を実行し、再起動します。
merge中は先に `git merge --abort`、stashを使った場合は復元漏れも確認します。

## 停止

Muse側の受信・監視フックを先に解除し、その後 `monitor.pid` のプロセスを停止します。
フックが残ると再起動される場合があります。Botのアクセスを失効させる場合はPortalでトークンを再発行します。

## 困ったとき

| 症状 | 確認・対応 |
| --- | --- |
| 設定エラー | `uv run python bin/settings.py validate` の指摘を直す |
| トークン拒否・Message Content不足 | トークンをログへ出さず、`bin/connect-discord.py --saved` の案内に従う |
| キューが増えるが返答しない | 受信フックの登録先、専用ワーカー、`worker-prompt.md` のパスを確認 |
| フック未実行・古い指示 | [導入手順](SETUP.md) の登録を確認し、プロンプトを再生成 |
| チャンネルが見えない・投稿できない | 招待状態とBotロールの閲覧・送信権限を確認。本人DM用ではサーバー無応答が正常 |
| `/settings`・ボタンが動かない | `monitor.log` の登録エラー、`settings_views/`、Interactions Endpoint URLが空かを確認 |
| 重複返信を抑止したログ | 受信フックと別のポーラーを二重登録していないか確認 |
| 外部アプリが拒否される | [権限段階・nonce・承認結果](EXTERNAL_APPS.md) を確認。結果不明の操作は再送前にアプリ側を確認 |

ログは `monitor.log`・`watchdog.log`、メッセージの処理結果は `mention_ledger.json` にあります。
