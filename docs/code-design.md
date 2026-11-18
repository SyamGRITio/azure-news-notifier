# コードの設計意図

## 最初に読む処理

Azureが起動する入口は `app/function_app.py` の `daily_news` です。そこから `run_azure`、`notifier/core.py` の `run` を呼びます。別ファイルへ分けた処理も、すべて同じFunction AppのPythonプロセスで実行されます。

```text
毎朝8時のタイマー
  → Azureの設定・マネージドID・Blob保存先を準備
  → 登録RSSを取得して記事へ変換
  → 開始日時と送信履歴で対象を選ぶ
  → 必要な記事を日本語に翻訳
  → Slack・LINEへ送信し、宛先ごとに成功を保存
  → Slackへ処理結果を通知
```

## ファイルを分けた理由

| ファイル（app/配下） | 役割と、ここに置く理由 |
|---|---|
| `function_app.py` | Azureの入口。環境変数・認証・Blobの排他を用意し、配信本体へ渡す。 |
| `notifier/core.py` | 配信の順序と判断。通信や保存の関数を引数で受け取るため、Azureなしでも判断を検証できる。 |
| `notifier/models.py` | 記事・送信履歴・配信結果の項目を定義する。複数の処理で同じ意味のデータを使う。 |
| `notifier/feed_parser.py` | RSS/AtomのXMLを記事へ変換する。日付・HTML除去・URLの確認もここに集める。 |
| `notifier/translation.py` | Foundryへの要求と応答の確認。モデルの仕様を配信順序から分離する。 |
| `notifier/notifications/slack.py` | Slack用の見た目と送信。プレビューや結果通知はここで決める。 |
| `notifier/notifications/line.py` | LINE用の本文と送信。記事だけを送り、処理結果は送らない。 |
| `notifier/http_client.py` | 通信の待ち時間・応答サイズ・転送制限を共有する。 |
| `notifier/state_store.py` | 同じ状態を、ローカルはJSON、AzureはBlobへ保存する。Azureでは同時実行を防ぐleaseを使う。 |

`__init__.py` はPythonのパッケージを示すファイルなので、空でも役割があります。

`local_check.py` と `notifier/__main__.py` はローカル確認用です。`.funcignore` でAzureへの配置から除外しています。

## 記事と送信履歴

`feeds.json` は取得元RSSの一覧です。`state.json` は取得元の登録には使わず、送信対象の判断と翻訳枠の管理に使います。

| 状態の項目 | 意味 |
|---|---|
| `month` | 翻訳枠を管理する月。 |
| `translation_attempts` | 今月予約した翻訳の呼び出し回数。APIを呼ぶ前に保存し、失敗時も戻さない。 |
| `delivered_urls` | 次回のSlack配信から除外するURL。 |
| `line_delivered_urls` | 次回のLINE配信から除外するURL。LINE初回有効化時には既存Slack履歴を引き継ぎ、過去分の一括送信を避ける。 |

LINE履歴には導入時に引き継いだ記事も含まれるため、この一覧だけで実際のLINE受信を証明することはできません。送信処理の成功もAPI受付までの確認であり、端末の受信・既読とは別です。

Slackだけ成功した場合はその成功を保存し、次回はLINEだけを再試行します。送信受付直後に通信や保存が途切れた場合は、再送時に重複する可能性が残ります。

## 新着なし・失敗・上限の違い

- 登録RSSは最大9サイト。記事の配信は1回最大10件で、翻訳は月30回まで。
- `DELIVERY_START_AT` は固定の配信開始日時。毎日の開始時刻ではない。
- 全RSSを確認でき、対象記事がなければ「新しい記事はありません」と通知する。
- 一部のRSSが確認できなければ、その事実を通知する。全サイトに新着がないとは断定しない。
- 翻訳失敗や翻訳上限では原文を配信する。失敗と上限を分けて記録する。
- 正常時のサイト数・宛先別件数はログに記録し、Slackの結果通知には並べない。

## 開発用の生成物

`__pycache__`、`.pytest_cache`、`.ruff_cache`、`*.egg-info` は開発ツールの生成物です。ソースコードではなく、Git・Azureへの配置から除外し、VS Codeでも非表示にしています。`.venv` はローカルの実行環境です。

CIはGitに登録されたアプリファイルだけをまとめ、配置時に `.funcignore` も適用します。Azure用の依存ライブラリは `app/requirements.txt`、開発用は `app/pyproject.toml` で管理します。

## Azure側の責任範囲

アプリ自身が動いている範囲で、取得・翻訳・記事送信の一部失敗をSlackへ通知します。Function全体が起動しない場合やSlack自体の障害は、この通知だけでは検知できません。毎朝08:45のLogic Appsが完了ログの有無を確認し、完了未確認または検索失敗をSlackへ通知します。Logic Apps・Key Vault・Slack自体の障害時に通知できない限界があります。
