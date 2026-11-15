"""日次配信結果をSlack Block Kitのメッセージへ変換する。"""

from typing import Any
from urllib.parse import urlsplit

from ..http_client import HttpClient, validate_https_url
from ..models import Digest


def build_payload(digest: Digest) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": digest.heading}}
    ]
    for article in digest.articles:
        # 記事データはplain_textにし、メンションやMarkdownとして解釈させません。
        text = f"{article.title[:200]}\n{article.summary[:500]}".strip()
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": text}})
        safe_url = article.url.replace("&", "&amp;").replace("|", "%7C")
        safe_url = safe_url.replace("<", "%3C").replace(">", "%3E")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<{safe_url}|記事を読む>"}}
        )
    if digest.notices:
        blocks.append(
            {"type": "section", "text": {"type": "plain_text", "text": "\n".join(digest.notices)}}
        )
    return {
        "text": digest.heading,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


class SlackNotifier:
    def __init__(self, webhook: str, http: HttpClient) -> None:
        validate_https_url(webhook)
        if urlsplit(webhook).hostname != "hooks.slack.com":
            raise ValueError("Expected a Slack Incoming Webhook")
        self._webhook = webhook
        self._http = http

    def send(self, digest: Digest) -> None:
        response = self._http.post_json(self._webhook, build_payload(digest))
        if response.strip() != b"ok":
            raise RuntimeError("Slack did not accept the message")
