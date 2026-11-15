# azure-news-notifier

Azure公式の更新情報や日本語サポートブログのRSSを収集し、英語の記事タイトル・概要を日本語に翻訳してSlackへ日次通知する個人プロジェクトです。

- 実行基盤: Azure Functions (Python)
- 翻訳: Azure OpenAI / Microsoft Foundry
- 構成管理: Bicep（検証後に追加）
- 状態: 設計・開発準備中（未デプロイ）

> この公開リポジトリにはAPIキー、Slack Webhook URL、サブスクリプションIDなどの秘密情報・環境固有情報を保存しません。
