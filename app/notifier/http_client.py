"""公開フィード、Foundry、Slack、LINEに共通のHTTP通信と制限。"""

import json
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 4_000_000


class RedirectBlockedError(ValueError):
    """配信元が別URLへの移動を要求した。"""


class ResponseTooLargeError(ValueError):
    """応答がアプリの読み込み上限を超えた。"""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectBlockedError("HTTP redirects are disabled")


def validate_https_url(url: str, *, host_suffix: str | None = None) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
        or parts.port not in (None, 443)
        or any(character.isspace() for character in url)
    ):
        raise ValueError("A valid HTTPS URL is required")
    if host_suffix and not parts.hostname.endswith(host_suffix):
        raise ValueError("Unexpected destination host")


class HttpClient:
    def get(self, url: str) -> bytes:
        return self._request(url)

    def post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> bytes:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request(url, body, {"Content-Type": "application/json", **(headers or {})})

    def _request(
        self, url: str, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> bytes:
        validate_https_url(url)
        request = Request(
            url,
            data=body,
            headers={"User-Agent": "azure-news-notifier/0.1", **(headers or {})},
        )
        # 別ホストへのリダイレクトに認証ヘッダーやWebhookを渡しません。
        with build_opener(_RejectRedirects()).open(request, timeout=30) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ResponseTooLargeError("HTTP response exceeds size limit")
        return data
