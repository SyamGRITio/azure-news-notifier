# 設計

RSSのタイトル・概要・リンクを収集し、英語の短文だけを翻訳して1日1回Slackへ配信します。
記事本文の取得・翻訳や検索DBは扱いません。

## 論理構成

![論理構成](images/logical.png)

<details>
<summary>編集用Mermaid</summary>

```mermaid
flowchart LR
    feeds["RSS / Atom<br/>9フィード"] --> collect["記事の収集"]
    collect --> dedup["配信済み・重複記事の除外"]
    state[("配信状態")] --> dedup
    dedup --> limits["日次件数と翻訳上限の確認"]
    limits --> language{"日本語のタイトルか"}
    language -->|はい| digest["日次メッセージの作成"]
    language -->|いいえ| translate["タイトル・RSS概要の翻訳"]
    translate -->|成功した翻訳文| digest
    translate -->|失敗した場合は原文| digest
    collect -->|取得失敗の情報| digest
    digest --> slack["Slackへまとめて送信"]
    slack -->|送信成功後に既読を保存| state
```

</details>

## Azure上の構成

![Azure上の構成](images/physical.png)

<details>
<summary>編集用Mermaid</summary>

```mermaid
flowchart TB
    subgraph azure["Azure"]
        timer["Timer Trigger<br/>毎日 00:00 UTC / 09:00 JST"]
        func["Azure Functions<br/>Python / Flex Consumption"]
        foundry["Microsoft Foundry<br/>gpt-5-nano / Global Standard"]
        vault["Key Vault Standard<br/>Slack Webhook"]
        storage["Storage Standard LRS<br/>ホスト・コード・配信状態"]
        timer --> func
        func -->|Managed Identityで翻訳APIを呼び出す| foundry
        vault -->|Key Vault Referenceでアプリ設定へ渡す| func
        func <-->|状態保存と排他制御| storage
    end
    rss["公開RSS / Atom"] -->|HTTPS| func
    func -->|HTTPS POST| slack["Slack Incoming Webhook"]
```

</details>

## 処理フロー

![処理フロー](images/processing.png)

<details>
<summary>編集用Mermaid</summary>

```mermaid
flowchart TD
    start["起動"] --> load["配信済みURL・翻訳回数を読む"]
    load --> month["月が変わっていれば翻訳回数をリセット"]
    month --> fetch["登録したRSS / Atomを取得"]
    fetch --> parsed{"取得・解析できたか"}
    parsed -->|いいえ| failure["失敗したフィードを記録"]
    parsed -->|はい| dedup["配信済みURLと今回の重複を除外"]
    failure --> next{"次のフィードがあるか"}
    dedup --> next
    next -->|はい| fetch
    next -->|いいえ| select["先頭から最大10記事を選ぶ"]
    select --> language{"各記事: 日本語タイトルか"}
    language -->|はい| original["原文を使う"]
    language -->|いいえ| budget{"月30回の翻訳枠が残っているか"}
    budget -->|いいえ| original
    budget -->|はい| reserve["翻訳回数を加算して保存"]
    reserve --> translate["Foundryで翻訳"]
    translate --> valid{"完了したJSONを取得できたか"}
    valid -->|いいえ| original
    valid -->|はい| translated["翻訳文を使う"]
    original --> collect["記事と通知事項をまとめる"]
    translated --> collect
    collect --> send["Slackへ送信"]
    send --> sent{"送信成功か"}
    sent -->|はい| save["選んだ記事のURLを配信済みとして保存"]
    sent -->|いいえ| failed["失敗終了・配信済みは更新しない"]
    save --> endnode["終了"]
```

</details>

## アプリの役割

`app/notifier/` が通常処理、`app/local_check.py` がローカル確認、`app/tests/` が自動テストです。

|通常処理のファイル|役割|
|---|---|
|core.py|収集→重複除外→翻訳→通知→既読保存の順番|
|feed_parser.py|RSS / AtomのXMLから記事を取り出す|
|translation.py|Foundryでタイトル・概要を日本語にする|
|notifications/slack.py|Slackの表示形式と送信。LINEは将来ここへ追加|
|state_store.py|配信済みURLと月間翻訳回数をJSONへ保存|
|http_client.py|HTTPS通信の共通制限|
|models.py|記事・配信状態・配信結果のデータ型|

デモ、RSSだけの確認、1記事の接続確認はlocal_check.pyへ分離しています。
既存の `python -m notifier` も互換の起動口として残しています。

## リソース設定

名前は公開用の例です。リソースの実名やIDはソースへ入れません。

|対象|名前の例|設定|
|---|---|---|
|Resource Group|rg-azure-news-dev|関連リソースをまとめる|
|Foundry|ai-azure-news-dev-xxxx|gpt-5-nano、2025-08-07、Global Standard、TPM 1000、DefaultV2|
|モデル更新|—|現在バージョンの有効期限が切れたら更新|
|Key Vault|kv-aznews-dev-xxxx|Standard、RBAC、Webhookをslack-webhookとして保管|
|Storage|staznewsdevxxxx|StorageV2、Standard LRS、匿名公開無効、HTTPS必須|
|Function App|func-azure-news-dev-xxxx|Linux / Python 3.12候補 / Flex Consumption、Always Ready 0|
|Timer|daily_news|`0 0 0 * * *`：UTC 00:00、JST 09:00|

FunctionsとBlobへの保存処理は未実装です。現状のローカル版はJSONファイルを使います。
Azure移行時は、状態をBlobへ保存し、leaseによる排他を追加します。

## 認証

|実行場所|Foundryを呼ぶ主体|必要な権限|
|---|---|---|
|ローカル|Azure CLI等でログインした開発者|対象FoundryのCognitive Services OpenAI User|
|Azure Functions|FunctionsのManaged Identity|同じロールをFunctions自身へ割り当てる|

開発者の認証情報をFunctionsへコピーしません。
FunctionsのWebhookはKey Vault Referenceで渡し、IDにはKey Vault Secrets Userを付与します。
状態BlobにはStorage Blob Data Contributorを付与します。Functionsホスト用Storageの設定・権限は別途必要です。

## 上限と失敗時の動作

- 9フィード、1回最大10記事、翻訳は失敗を含め月30回。月の区切りはJST。
- 翻訳入力はタイトル120文字・概要300文字、出力400トークン。推論分も出力に含む。
- HTTPは30秒、最大4MB。危険なXMLを拒否し、記事本文へはアクセスしない。
- 日本語タイトルは翻訳しない。取得したURLの完全一致で重複を除外する。
- 翻訳回数はAPIの前に保存。既読はSlack送信の後に保存する。
- 通常配信の翻訳失敗は原文で継続。接続確認では翻訳失敗時に送信せず停止する。
- 初回はRSS内の過去記事も対象。上限超過分は次回のRSSに残っていれば再取得する。
- 送信直後の保存失敗では重複の可能性がある。状態破損時は処理を停止する。
- アプリ停止やSlack自体の障害は同じSlackへ通知できない。独立監視は追加設計が必要。

## 料金の目安

月30記事、入力500・出力400トークン/記事、入力$0.05・出力$0.40/100万トークン、1ドル150円を仮定すると、翻訳は約0.83円/月です。
仮単価・税別の計算例であり、契約価格は構築時に確認します。
Functionsの無料枠が残り、Storage 0.1GB・月10,000操作、Key Vault月100操作を仮定した従来の概算は全体約8.8円/月です。
Storage操作などは未実測で、月50円以内の保証ではありません。Always Ready、有料の独立監視、多量のログは含めません。

参考（日本語）: [Functions](https://learn.microsoft.com/ja-jp/azure/azure-functions/flex-consumption-plan)、[Key Vault Reference](https://learn.microsoft.com/ja-jp/azure/app-service/app-service-key-vault-references)、[Foundry API](https://learn.microsoft.com/ja-jp/azure/foundry/openai/api-version-lifecycle)、[料金表](https://azure.microsoft.com/ja-jp/pricing/details/azure-openai/)。
