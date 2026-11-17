import json

import pytest

from notifier.core import run
from notifier.models import Article, Digest
from notifier.notifications.slack import SlackNotifier, build_payload
from notifier.state_store import JsonStateStore
from notifier.translation import FoundryTranslator


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.finish_reason = "stop"

    def get(self, url):
        return b"""<rss><channel><item><title>Update</title>
        <link>https://example.com/a</link><description>Summary</description></item></channel></rss>"""

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload, headers))
        if url.startswith("https://hooks.slack.com"):
            return b"ok"
        return json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": self.finish_reason,
                        "message": {"content": json.dumps({"title": "更新", "summary": "概要"})},
                    }
                ]
            }
        ).encode()


def test_feed_translation_slack_and_persistent_state(tmp_path):
    http = FakeHttp()
    store = JsonStateStore(tmp_path / "state.json")
    translator = FoundryTranslator(
        "https://example.openai.azure.com", "gpt-5-nano", lambda: "fake-token", http
    )
    notifier = SlackNotifier("https://hooks.slack.com/services/TEST", http)
    digest = run(
        ["https://example.com/feed"],
        http.get,
        translator.translate,
        notifier.send,
        store.load(),
        "2026-11",
        store.save,
    )
    assert digest.articles[0].title == "更新"
    assert store.load().translation_attempts == 1
    assert store.load().delivered_urls == {"https://example.com/a"}
    assert len(http.calls) == 3
    assert http.calls[0][2] == {"Authorization": "Bearer fake-token"}
    assert http.calls[0][1]["max_completion_tokens"] == 400
    assert http.calls[1][1]["blocks"][1]["text"]["text"] == "<https://example.com/a|更新>"
    assert http.calls[1][1]["blocks"][2]["text"]["text"] == "概要"
    assert http.calls[2][1]["text"] == "【Azure Functions】記事を1件配信しました。"


def test_incomplete_translation_is_rejected():
    http = FakeHttp()
    http.finish_reason = "length"
    translator = FoundryTranslator(
        "https://example.openai.azure.com", "gpt-5-nano", lambda: "x", http
    )
    with pytest.raises(ValueError):
        translator.translate(Article("A", "B", "https://example.com"))


def test_translation_spacing_without_waiting(monkeypatch):
    http = FakeHttp()
    clock = iter([100.0, 110.0, 161.0])
    sleeps = []
    monkeypatch.setattr("notifier.translation.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("notifier.translation.time.sleep", sleeps.append)
    translator = FoundryTranslator(
        "https://example.openai.azure.com", "gpt-5-nano", lambda: "x", http
    )
    article = Article("A", "B", "https://example.com")
    translator.translate(article)
    translator.translate(article)
    assert sleeps == [51.0]


def test_slack_keeps_long_links_and_untrusted_text_in_separate_blocks():
    article = Article("<!channel>" * 20, "<&>" * 160, "https://example.com/" + "a" * 1900)
    payload = build_payload(Digest((article,)))
    assert payload["blocks"][2]["text"]["type"] == "plain_text"
    assert article.url in payload["blocks"][1]["text"]["text"]
    assert "<!channel>" not in payload["blocks"][1]["text"]["text"]
    assert len(payload["blocks"][2]["text"]["text"]) == 180
    assert len(payload["blocks"]) == 3
    assert all(len(block["text"]["text"]) <= 3000 for block in payload["blocks"] if "text" in block)


@pytest.mark.parametrize(
    "data",
    [[], {}, {"month": "2026-11", "translation_attempts": -1, "delivered_urls": []}],
)
def test_corrupt_state_stops_instead_of_resetting(tmp_path, data):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        JsonStateStore(path).load()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.openai.azure.com",
        "https://example.com",
        "https://example.openai.azure.com/other",
        "https://example.openai.azure.com?query=1",
    ],
)
def test_rejects_invalid_foundry_endpoint(url):
    with pytest.raises(ValueError):
        FoundryTranslator(url, "gpt-5-nano", lambda: "x", FakeHttp())


def test_slack_failure_is_not_accepted():
    class FailedHttp(FakeHttp):
        def post_json(self, url, payload, headers=None):
            return b"invalid_payload"

    with pytest.raises(RuntimeError):
        SlackNotifier("https://hooks.slack.com/services/TEST", FailedHttp()).send(Digest(()))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.services.ai.azure.com",
        "https://example.services.ai.azure.com/api/projects/example-project",
        "https://example.openai.azure.com/openai/v1/",
    ],
)
def test_supported_foundry_endpoint_forms(endpoint):
    http = FakeHttp()
    translator = FoundryTranslator(endpoint, "gpt-5-nano", lambda: "x", http)
    translator.translate(Article("A", "B", "https://example.com"))
    assert http.calls[0][0].endswith("/openai/v1/chat/completions")
    assert "/api/projects/" not in http.calls[0][0]


def test_foundry_project_uses_verified_openai_compatible_route():
    http = FakeHttp()
    translator = FoundryTranslator(
        "https://example.services.ai.azure.com/api/projects/project",
        "gpt-5-nano",
        lambda: "x",
        http,
    )
    translator.translate(Article("A", "B", "https://example.com"))
    assert http.calls[0][0] == "https://example.openai.azure.com/openai/v1/chat/completions"


def test_separate_slack_posts_checkpoint_each_article_and_retry_only_failed_one(
    tmp_path,
):
    from datetime import datetime

    class PartialHttp(FakeHttp):
        fail_second = True

        def get(self, url):
            return (
                "<rss><channel>"
                "<item><title>更新A</title><link>https://example.com/a</link>"
                "<pubDate>2026-11-16T01:00:00+09:00</pubDate></item>"
                "<item><title>更新B</title><link>https://example.com/b</link>"
                "<pubDate>2026-11-16T02:00:00+09:00</pubDate></item>"
                "</channel></rss>"
            ).encode()

        def post_json(self, url, payload, headers=None):
            self.calls.append((url, payload, headers))
            if self.fail_second and any(
                block.get("text", {}).get("text", "").startswith("<https://example.com/b|")
                for block in payload["blocks"]
            ):
                return b"invalid_payload"
            return b"ok"

    http = PartialHttp()
    notifier = SlackNotifier("https://hooks.slack.com/services/TEST", http)
    store = JsonStateStore(tmp_path / "state.json")
    start = datetime.fromisoformat("2026-11-16T00:00:00+09:00")
    now = datetime.fromisoformat("2026-11-17T08:00:00+09:00")
    with pytest.raises(RuntimeError):
        run(
            ["feed"],
            http.get,
            lambda a: pytest.fail("Japanese article"),
            notifier.send,
            store.load(),
            "2026-11",
            store.save,
            start_at=start,
            now=now,
        )
    assert store.load().delivered_urls == {"https://example.com/a"}
    assert "一部の記事を配信できませんでした" in http.calls[-1][1]["text"]
    assert "Slack送信失敗" in http.calls[-1][1]["text"]

    http.calls.clear()
    http.fail_second = False
    result = run(
        ["feed"],
        http.get,
        lambda a: a,
        notifier.send,
        store.load(),
        "2026-11",
        store.save,
        start_at=start,
        now=now,
    )
    assert len(http.calls) == 2  # 未送信の記事Bと、最後の結果通知。
    assert result.articles[0].url == "https://example.com/b"
    assert store.load().delivered_urls == {
        "https://example.com/a",
        "https://example.com/b",
    }


def test_article_previews_enabled_but_result_preview_disabled():
    article = Article("更新", "概要", "https://example.com/a")
    payload = build_payload(Digest((article,)))
    assert payload["unfurl_links"] is True
    assert payload["unfurl_media"] is True
    for result in [Digest(()), Digest((article,), is_summary=True)]:
        payload = build_payload(result)
        assert payload["unfurl_links"] is False
        assert payload["unfurl_media"] is False


def test_zero_articles_is_clear_and_does_not_imply_line_was_called():
    digest = Digest((), total_feeds=9, is_summary=True, line_enabled=True)
    payload = build_payload(digest)
    assert payload["text"] == "【Azure Functions】新しい記事はありません。"
    assert "0/9" not in payload["text"]
    assert "送信成功" not in payload["text"]


def test_only_actual_problems_are_shown_in_result():
    article = Article("更新", "概要", "https://example.com/a")
    digest = Digest(
        (article,),
        notices=("LINE送信失敗: 未送信の記事は次回再試行",),
        total_feeds=9,
        delivery_failures=1,
        is_summary=True,
        line_enabled=True,
    )
    payload = build_payload(digest)
    assert "一部の記事を配信できませんでした" in payload["text"]
    assert "LINE送信失敗" in payload["text"]
    assert "取得失敗：0" not in payload["text"]


def test_all_feeds_failed_is_not_reported_as_no_new_articles():
    text = build_payload(Digest((), failed_feeds=9, total_feeds=9, is_summary=True))["text"]
    assert "記事を取得できませんでした" in text
    assert "新しい記事はありません" not in text


@pytest.mark.parametrize("failed_channel", ["Slack", "LINE"])
def test_destination_checkpoint_retries_only_failed_channel(tmp_path, failed_channel):
    xml = "<rss><channel><item><title>更新</title><link>https://example.com/a</link></item></channel></rss>".encode()
    store = JsonStateStore(tmp_path / "state.json")
    calls = {"Slack": [], "LINE": []}
    should_fail = True

    def receiver(name):
        def send(digest):
            if digest.is_summary:
                return
            calls[name].append(digest.articles[0].url)
            if should_fail and name == failed_channel:
                raise OSError("Unavailable")

        return send

    with pytest.raises(OSError):
        run(
            ["feed"],
            lambda _: xml,
            lambda a: a,
            receiver("Slack"),
            store.load(),
            "2026-11",
            store.save,
            send_line=receiver("LINE"),
        )
    should_fail = False
    run(
        ["feed"],
        lambda _: xml,
        lambda a: a,
        receiver("Slack"),
        store.load(),
        "2026-11",
        store.save,
        send_line=receiver("LINE"),
    )
    assert len(calls[failed_channel]) == 2
    successful_channel = "Slack" if failed_channel == "LINE" else "LINE"
    assert len(calls[successful_channel]) == 1
    assert store.load().delivered_urls == {"https://example.com/a"}
    assert store.load().line_delivered_urls == {"https://example.com/a"}


def test_line_sends_article_with_recipient_and_does_not_send_summary():
    from notifier.notifications.line import LineNotifier

    class LineHttp:
        calls = []

        def post_json(self, url, payload, headers=None):
            self.calls.append((url, payload, headers))
            return b"{}"

    http = LineHttp()
    notifier = LineNotifier("fake-token", "U" + "0" * 32, http)
    notifier.send(Digest((), is_summary=True))
    assert http.calls == []
    article = Article("更新", "概要", "https://example.com/a")
    notifier.send(Digest((article,)))
    assert len(http.calls) == 1
    assert http.calls[0][1]["to"] == "U" + "0" * 32
    assert article.url in http.calls[0][1]["messages"][0]["text"]
    assert http.calls[0][2] == {"Authorization": "Bearer fake-token"}
