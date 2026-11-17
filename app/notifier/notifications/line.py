"""LINE Messaging APIへ記事だけを送る。配信結果・失敗通知はSlackが担当する。"""

import json
import re

from ..http_client import HttpClient
from ..models import Digest


class LineNotifier:
    def __init__(self, access_token: str, recipient_id: str, http: HttpClient) -> None:
        if not access_token.strip() or not re.fullmatch(r"[UCR][0-9a-fA-F]{32}", recipient_id):
            raise ValueError("LINE token and recipient ID are required")
        self._token = access_token
        self._recipient = recipient_id
        self._http = http

    def send(self, digest: Digest) -> None:
        if digest.is_summary or not digest.articles:
            return
        if len(digest.articles) != 1:
            raise ValueError("Send one article per LINE message")
        article = digest.articles[0]
        source = (
            "【動作確認｜実RSS記事】" if digest.test_message else "【Azure Functions｜RSS配信】"
        )
        text = f"{source}\n{article.title[:200]}\n{article.summary[:180]}\n{article.url}"
        response = self._http.post_json(
            "https://api.line.me/v2/bot/message/push",
            {"to": self._recipient, "messages": [{"type": "text", "text": text}]},
            {"Authorization": "Bearer " + self._token},
        )
        if not isinstance(json.loads(response), dict):
            raise RuntimeError("Unexpected LINE response")
