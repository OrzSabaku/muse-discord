# オフラインテスト

Discordや外部サービスを使わず、API・Gatewayを偽物へ置き換えて検証します。書き込み先は一時ディレクトリです。
実Discordの試験は [e2e/README.md](../e2e/README.md) を参照してください。

## 実行

```bash
bash tests/run_all.sh                    # 全スイート、並列数4
bash tests/run_all.sh settings ownerdm   # 対象を指定
bash tests/run_all.sh --skip-slow -j 8
MUT_DIR=/path/to/tree python3 tests/locking/test_locking.py
python3 -m unittest discover -s tests -p test_personal_profile.py
```

Python 3.10以上、PyYAML、bash、git、jq、unshareが必要です。
watchdog系はユーザー・PID名前空間を使います。UbuntuのAppArmor設定で制限される場合は名前空間の許可が必要です。
動画テストはffmpegがないとスキップします。遅いスイートには `SLOW` があります。

MCP・承認検証はPython 3.11以上とオプション依存を使います。依存がない場合は該当テストをスキップします。

```bash
.venv-mcp/bin/pip install -r requirements-apps.txt
.venv-mcp/bin/python tests/discordtools/test_mcp.py
.venv-mcp/bin/python tests/externalapps/test_broker.py
.venv-mcp/bin/python tests/externalapps/test_transport.py
node --test telemetry/worker/test/worker.test.js
```

## テストの追加

- `tests/<suite>/test_*.py` を作る。runnerが自動検出し、各ファイルを個別のunittestとして実行する。
- `_lib/common.py` の `MUT_DIR`・`scratch()`、必要に応じてモニター用harnessを使う。実ファイルや実トークンへ触れない。
- 対象は誤応答、権限、秘密情報、データ消失、競合、再起動、API制限。文言・表示順・実装の写しをテストしない。
- 実時間の待機を避け、時計や間隔を差し替える。既存のシナリオへ検証を追加できるか先に確認する。
- 変更したスイート、`ruff check --select E9,F`、シェル変更時はShellCheckで確認する。

導入用の `onboarding/fixtures/persona.md` は、上流 `7bd0706621b791a5adc72a94d06725ed993f406f` の原本です。
ハッシュでプレースホルダーを判定するため内容は変更せず、一時的なパッケージへコピーして使います。
