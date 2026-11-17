"""記事は1件ずつコンパクトに、配信結果は別メッセージとしてSlackへ送る。"""

import time
from html import escape
from typing import Any
from urllib.parse import urlsplit

from ..http_client import HttpClient, validate_https_url
from ..models import Digest


def build_payload(digest: Digest) -> dict[str, Any]:
    if digest.is_summary or not digest.articles:
        lines = ["【Azure Functions｜配信結果】", digest.heading]
        if digest.line_enabled:
            lines.append(f"送信成功：Slack {digest.sent_slack}件 / LINE {digest.sent_line}件")
        lines.append(
            f"RSS取得失敗：{digest.failed_feeds}/{digest.total_feeds}件 / "
            f"翻訳失敗：{digest.translation_failures}件"
        )
        if digest.delivery_failures:
            lines.append("一部の送信先に届けられませんでした。成功済みの送信先へは再送しません。")
        lines.extend(digest.notices)
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
