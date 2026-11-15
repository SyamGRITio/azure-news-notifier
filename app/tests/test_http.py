import pytest

from notifier.http_client import HttpClient, _RejectRedirects, validate_https_url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com:8443",
        "https://example.com/#fragment",
        "https://example.com/white space",
    ],
)
def test_rejects_unsafe_connection_urls(url):
    with pytest.raises(ValueError):
        validate_https_url(url)


def test_redirects_are_rejected_without_forwarding_headers():
    with pytest.raises(ValueError):
        _RejectRedirects().redirect_request(None, None, 302, "Found", {}, "https://other.com")


def test_post_uses_timeout_and_bounded_response(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            observed["read_size"] = size
            return b"ok"

    class Opener:
        def open(self, request, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return Response()

    monkeypatch.setattr("notifier.http_client.build_opener", lambda *_: Opener())
    assert HttpClient().post_json("https://example.com", {"text": "更新"}) == b"ok"
    assert observed["timeout"] == 30
    assert observed["read_size"] == 4_000_001
    assert observed["request"].get_method() == "POST"
    assert "更新" in observed["request"].data.decode()


def test_oversized_http_response_is_rejected(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b"x" * size

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr("notifier.http_client.build_opener", lambda *_: Opener())
    with pytest.raises(ValueError):
        HttpClient().get("https://example.com/feed")
