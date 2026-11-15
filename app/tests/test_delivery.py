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
    assert len(http.calls) == 2
    assert http.calls[0][2] == {"Authorization": "Bearer fake-token"}
    assert http.calls[0][1]["max_completion_tokens"] == 400
    assert http.calls[1][1]["blocks"][1]["text"]["text"] == "更新\n概要"


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
    assert payload["blocks"][1]["text"]["type"] == "plain_text"
    assert article.url in payload["blocks"][2]["text"]["text"]
    assert all(len(block["text"]["text"]) <= 3000 for block in payload["blocks"])


@pytest.mark.parametrize(
    "data", [[], {}, {"month": "2026-11", "translation_attempts": -1, "delivered_urls": []}]
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
