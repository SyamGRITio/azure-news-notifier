# Azure News Notifier

Azure公式更新・サポートブログのRSSを毎朝08:00（日本時間）に確認し、英語のタイトルとRSS概要を日本語にしてSlack・LINEへ配信します。記事全文は取得・翻訳しません。小規模な個人利用を想定し、月50円以内を目標にしています。

## RSSとは

RSS / Atomは、サイトがXMLで公開する記事一覧です。このアプリが定期的に一覧を取りに行く「ポーリング」で更新を確認します。サイトから通知がプッシュされる仕組みではありません。

登録先は、WebページのURLではなく `app/feeds.json` のRSS / Atom URLです。配信開始日時と送信履歴で対象を絞り、同じ記事の再配信を避けます。

![処理の概要](docs/images/overview.png)

## 読む順番

1. [設計](docs/design.md)：構成図、Azureの設定、認証、監視と料金の前提。
2. [コードの設計意図](docs/code-design.md)：処理の順番、ファイルを分けた理由、配信履歴JSONの意味。
3. `app/function_app.py` → `app/notifier/core.py`：Azureの入口から配信本体を読む。

## ファイルの配置

| 場所 | 内容 |
|---|---|
| `app/` | Azureへ配置するアプリ。`feeds.json` で取得元を変更する。 |
| `tests/` | 単体・モック結合テスト。Azureへの配置対象には含めない。 |
| `.github/workflows/` | GitHub上のテストとdev環境へのデプロイ。 |
| `docs/` | 設計とコードの説明。 |

文書を先にレビューするため、文書だけのブランチにはアプリが含まれません。上の配置はアプリ取り込み後の構成です。

## ローカルで確認する

Python 3.12を使います。以下はリポジトリ直下で実行するPowerShellの例です。テストにAzure認証や通知先の設定は不要です。

```powershell
python -m venv app/.venv
app/.venv/Scripts/python -m pip install -e './app[dev,azure]'
app/.venv/Scripts/python -B -m pytest -c app/pyproject.toml tests -q -p no:cacheprovider
```

`.venv` はこのアプリ専用のPythonライブラリ置き場です。仮想マシンではなく、GitやAzureの配置対象にも含めません。

通常配信の入口はAzure Functionsの `daily_news` です。`app/local_check.py` はローカル確認専用で、実接続・実送信を行う操作もあるため、引数は `--help` で確認してください。

## 設定と現在の範囲

接続設定は環境変数で渡します。`app/.env.example` は設定名の見本です。実際のWebhook・トークンはGitへ登録せず、AzureではKey Vaultを使います。

FunctionsからSlack・LINEへの記事配信、Blobへの履歴保存、毎朝08:45のLogic Apps監視をdev環境で構築しています。Logic Appsは現在Portalで管理しており、Bicepによる再現はこれから追加する範囲です。

新着が0件の場合はSlackだけに結果を通知します。LINEには記事だけを送ります。
