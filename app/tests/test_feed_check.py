from local_check import check_feeds
from notifier.http_client import RedirectBlockedError, ResponseTooLargeError


def test_distinguishes_valid_empty_and_invalid_feeds():
    data = {
        "ok": b"<rss><channel><item><title>A</title><link>https://example.com</link></item></channel></rss>",
        "empty": b"<rss><channel/></rss>",
        "invalid": b"<html>not RSS</html>",
    }
    results = check_feeds(list(data), data.__getitem__)
    assert [result.succeeded for result in results] == [True, True, False]
    assert [result.article_count for result in results] == [1, 0, None]


def test_connection_failure_does_not_stop_other_feeds_or_print_exception():
    def fetch(url):
        if url == "bad":
            raise OSError("private details")
        return b"<rss><channel/></rss>"

    results = check_feeds(["bad", "ok"], fetch)
    assert not results[0].succeeded and results[1].succeeded
    assert "private details" not in results[0].detail


def test_check_feeds_mode_only_fetches_rss(monkeypatch, tmp_path, capsys):
    import local_check as cli

    feeds = tmp_path / "feeds.json"
    feeds.write_text('["https://example.com/rss"]', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["notifier", "--check-feeds", "--feeds", str(feeds)])
    monkeypatch.setattr("notifier.http_client.HttpClient.get", lambda *_: b"<rss><channel/></rss>")
    monkeypatch.setattr(cli, "_run_live", lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert cli.main() == 0
    assert "1/1件成功" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == [feeds]


def test_transport_limits_have_actionable_messages():
    def fetch(url):
        if url == "redirect":
            raise RedirectBlockedError()
        raise ResponseTooLargeError()

    results = check_feeds(["redirect", "large"], fetch)
    assert "転送を拒否" in results[0].detail
    assert "4MB" in results[1].detail


def test_http_status_is_reported_without_exception_body():
    from urllib.error import HTTPError

    def fetch(url):
        raise HTTPError(url, 404, "private details", None, None)

    result = check_feeds(["https://example.com/feed"], fetch)[0]
    assert result.detail == "HTTP 404"
