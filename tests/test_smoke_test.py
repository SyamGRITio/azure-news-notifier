from dataclasses import replace

import pytest

from local_check import SmokeTestError, run_smoke_test
from notifier.models import Article, DeliveryState

RSS = b"""<rss><channel><item><title>English update</title>
<link>https://example.com/a</link><description>Summary</description></item>
<item><title>Second</title><link>https://example.com/b</link></item></channel></rss>"""


def test_smoke_sends_one_translated_article_and_saves_budget_before_translation():
    state = DeliveryState()
    events = []

    def save(current):
        events.append(("save", current.translation_attempts, set(current.delivered_urls)))

    def translate(article):
        events.append(("translate", article.url))
        return replace(article, title="日本語の更新", summary="概要")

    result = run_smoke_test(
        ["feed"],
        lambda _: RSS,
        translate,
        lambda digest: events.append(("send", digest)),
        state,
        "2026-11",
        save,
    )
    assert len(result.articles) == 1
    assert result.articles[0].title == "日本語の更新"
    assert events[0] == ("save", 1, set())
    assert [event[0] for event in events] == ["save", "translate", "send", "save"]
    assert state.delivered_urls == {"https://example.com/a"}


def test_translation_failure_is_not_silently_sent_as_original():
    state = DeliveryState()
    sent = []

    def fail(article):
        raise RuntimeError("secret details")

    with pytest.raises(SmokeTestError, match="Foundry翻訳") as error:
        run_smoke_test(["feed"], lambda _: RSS, fail, sent.append, state, "2026-11", lambda _: None)
    assert "secret details" not in str(error.value)
    assert sent == [] and state.delivered_urls == set()
    assert state.translation_attempts == 1


def test_send_failure_does_not_mark_article_delivered():
    state = DeliveryState()

    def fail(digest):
        raise RuntimeError("secret webhook")

    with pytest.raises(SmokeTestError, match="Slack送信"):
        run_smoke_test(["feed"], lambda _: RSS, lambda a: a, fail, state, "2026-11", lambda _: None)
    assert not state.delivered_urls


def test_month_limit_blocks_translation():
    state = DeliveryState("2026-11", 30)
    with pytest.raises(SmokeTestError, match="上限"):
        run_smoke_test(
            ["feed"],
            lambda _: RSS,
            lambda _: pytest.fail(),
            lambda _: pytest.fail(),
            state,
            "2026-11",
            lambda _: None,
        )


def test_checkpoint_failure_stops_before_billable_call():
    def fail(state):
        raise OSError("disk unavailable")

    with pytest.raises(OSError):
        run_smoke_test(
            ["feed"],
            lambda _: RSS,
            lambda _: pytest.fail(),
            lambda _: pytest.fail(),
            DeliveryState(),
            "2026-11",
            fail,
        )


def test_smoke_selects_unread_english_article():
    japanese = RSS.replace(b"English update", "日本語の更新".encode())
    result = run_smoke_test(
        ["feed"],
        lambda _: japanese,
        lambda a: a,
        lambda _: None,
        DeliveryState(),
        "2026-11",
        lambda _: None,
    )
    assert result.articles == (Article("Second", "", "https://example.com/b"),)
