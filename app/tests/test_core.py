import pytest

from notifier.core import run
from notifier.models import Article, DeliveryState

RSS = b"""<rss><channel><item><title>Hello</title>
<link>https://example.com/a</link><description>Summary</description>
</item></channel></rss>"""


def keep_original(article):
    return article


def ignore_state(state):
    pass


def test_partial_failure_fallback_and_duplicate_suppression():
    state = DeliveryState()
    messages = []

    def fetch(url):
        if url == "bad":
            raise OSError("private details")
        return RSS

    def translate(article):
        raise RuntimeError("secret")

    digest = run(
        ["ok", "ok", "bad"],
        fetch,
        translate,
        messages.append,
        state,
        "2026-11",
        ignore_state,
    )
    assert digest.articles == (Article("Hello", "Summary", "https://example.com/a"),)
    assert digest.failed_feeds == 1
    assert digest.status == "一部失敗・制限あり"
    assert "secret" not in str(digest) and "private details" not in str(digest)

    second = run(["ok"], fetch, translate, messages.append, state, "2026-11", ignore_state)
    assert second.articles == ()
    assert state.translation_attempts == 1


def test_send_failure_leaves_articles_unread_and_keeps_translation_charge():
    state = DeliveryState()

    def fail(digest):
        raise OSError("Slack unavailable")

    with pytest.raises(OSError):
        run(["ok"], lambda _: RSS, keep_original, fail, state, "2026-11", ignore_state)
    assert not state.delivered_urls
    assert state.translation_attempts == 1


def test_monthly_limit_sends_original_and_next_month_resets_counter():
    state = DeliveryState(month="2026-11", translation_attempts=30)

    def unexpected_translation(article):
        pytest.fail("Translation must stop at monthly limit")

    digest = run(
        ["ok"],
        lambda _: RSS,
        unexpected_translation,
        lambda _: None,
        state,
        "2026-11",
        ignore_state,
    )
    assert "月間翻訳上限" in digest.notices[0]
    run(
        ["ok"],
        lambda _: RSS.replace(b"/a", b"/b"),
        keep_original,
        lambda _: None,
        state,
        "2026-12",
        ignore_state,
    )
    assert state.translation_attempts == 1


def test_daily_limit_does_not_mark_remaining_articles_as_read():
    xml = RSS.replace(
        b"</channel>", b"<item><title>B</title><link>https://example.com/b</link></item></channel>"
    )
    state = DeliveryState()
    digest = run(
        ["ok"],
        lambda _: xml,
        keep_original,
        lambda _: None,
        state,
        "2026-11",
        ignore_state,
        daily_limit=1,
    )
    assert len(digest.articles) == 1
    assert state.delivered_urls == {"https://example.com/a"}
    assert "未配信1件" in digest.notices[0]


def test_all_feeds_failed_still_sends_failure_digest():
    messages = []
    digest = run(
        ["bad"],
        lambda _: b"not xml",
        keep_original,
        messages.append,
        DeliveryState(),
        "2026-11",
        ignore_state,
    )
    assert digest.status == "収集失敗"
    assert len(messages) == 1


def test_japanese_article_does_not_consume_translation_budget():
    xml = RSS.replace(b"Hello", "更新情報".encode())
    state = DeliveryState()
    run(
        ["ok"],
        lambda _: xml,
        lambda _: pytest.fail("Unexpected translation"),
        lambda _: None,
        state,
        "2026-11",
        ignore_state,
    )
    assert state.translation_attempts == 0


def test_failed_budget_checkpoint_stops_before_calling_translation():
    calls = []

    def fail_save(state):
        raise OSError("Disk unavailable")

    with pytest.raises(OSError):
        run(
            ["ok"],
            lambda _: RSS,
            calls.append,
            calls.append,
            DeliveryState(),
            "2026-11",
            fail_save,
        )
    assert calls == []


@pytest.mark.parametrize(
    "feeds,daily,monthly", [([], 10, 30), (["x"] * 10, 10, 30), (["x"], 11, 30), (["x"], 10, 31)]
)
def test_reject_unsupported_limits(feeds, daily, monthly):
    with pytest.raises(ValueError):
        run(
            feeds,
            lambda _: RSS,
            keep_original,
            lambda _: None,
            DeliveryState(),
            "2026-11",
            ignore_state,
            daily_limit=daily,
            monthly_limit=monthly,
        )
