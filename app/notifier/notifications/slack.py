"""記事は1件ずつコンパクトに、配信結果は別メッセージとしてSlackへ送る。"""

import time
from html import escape
from typing import Any
from urllib.parse import urlsplit

from ..http_client import HttpClient, validate_https_url
from ..models import Digest


def build_payload(digest: Digest) -> dict[str, Any]:
    if digest.is_summary or not digest.articles:
        if digest.total_feeds and digest.failed_feeds == digest.total_feeds:
            message = "記事を取得できませんでした。"
        elif digest.delivery_failures:
            message = (
                "一部の記事を配信できませんでした。"
                if digest.articles
                else "記事を配信できませんでした。"
            )
        elif digest.articles:
            message = f"記事を{len(digest.articles)}件配信しました。"
        else:
            message = "新しい記事はありません。"
        lines = [f"【Azure Functions】{message}"]
        if digest.failed_feeds:
            lines.append(f"記事を取得できない配信元が{digest.failed_feeds}サイトあります。")
        if digest.translation_failures:
            lines.append(f"{digest.translation_failures}件は翻訳できず、原文で配信しました。")
        # 正常時の内訳はログへ。利用者には対応が必要な内容だけを知らせます。
        lines.extend(
            notice
            for notice in digest.notices
            if not notice.startswith(("RSS取得失敗:", "翻訳失敗:"))
        )
        text = "\n".join(lines)
        blocks = [{"type": "section", "text": {"type": "plain_text", "text": text}}]
    else:
        if len(digest.articles) != 1:
            raise ValueError("Send one article per Slack message")
        article = digest.articles[0]
        # タイトル内の記号でリンクやメンションを組み立てられないようにします。
        title = escape(article.title[:200].replace("|", "｜"), quote=False)
        safe_url = article.url.replace("&", "&amp;").replace("|", "%7C")
        safe_url = safe_url.replace("<", "%3C").replace(">", "%3E")
        source = (
            "【動作確認｜ローカル実送信・実RSS記事】"
            if digest.test_message
            else "【Azure Functions｜RSS配信】"
        )
        text = f"{source}\n{title}\n{safe_url}"
        blocks = [
            {"type": "context", "elements": [{"type": "plain_text", "text": source}]},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{safe_url}|{title}>",
                    "verbatim": True,
                },
            },
        ]
        if article.summary:
            summary = article.summary.strip()
            summary = summary[:179] + "…" if len(summary) > 180 else summary
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": summary,
                    },
                }
            )
    preview_enabled = bool(digest.articles) and not digest.is_summary
    return {
        "text": text,
        "blocks": blocks,
        "unfurl_links": preview_enabled,
        "unfurl_media": preview_enabled,
    }


class SlackNotifier:
    def __init__(self, webhook: str, http: HttpClient) -> None:
        validate_https_url(webhook)
        if urlsplit(webhook).hostname != "hooks.slack.com":
            raise ValueError("Expected a Slack Incoming Webhook")
        self._webhook = webhook
        self._http = http
        self._last_sent_at: float | None = None

    def send(self, digest: Digest) -> None:
        payload = build_payload(digest)
        # 記事を別々に投稿するため、同一通知先への連投間隔を空けます。
        if self._last_sent_at is not None:
            time.sleep(max(0, 1.1 - (time.monotonic() - self._last_sent_at)))
        response = self._http.post_json(self._webhook, payload)
        self._last_sent_at = time.monotonic()
        if response.strip() != b"ok":
            raise RuntimeError("Slack did not accept the message")
