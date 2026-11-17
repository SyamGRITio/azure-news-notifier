from datetime import datetime

import pytest

from notifier.core import run
from notifier.feed_parser import parse_feed
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
    assert digest.status == "一部失敗"
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
        b"</channel>",
        b"<item><title>B</title><link>https://example.com/b</link></item></channel>",
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
    "feeds,daily,monthly",
    [([], 10, 30), (["x"] * 10, 10, 30), (["x"], 11, 30), (["x"], 10, 31)],
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


START = datetime.fromisoformat("2026-11-16T00:00:00+09:00")
NOW = datetime.fromisoformat("2026-11-17T08:00:00+09:00")


def feed(*items):
    return (
        "<rss><channel>"
        + "".join(
            f"<item><title>日本語{i}</title><link>https://example.com/{i}</link>"
            + (f"<pubDate>{date}</pubDate>" if date else "")
            + "</item>"
            for i, date in items
        )
        + "</channel></rss>"
    ).encode()


@pytest.mark.parametrize(
    "tag,value,expected",
    [
        ("pubDate", "Sun, 15 Nov 2026 15:00:00 GMT", START),
        ("published", "2026-11-16T00:00:00+09:00", START),
        ("date", "2026-11-15T15:00:00Z", START),
        ("updated", "2026-11-16T00:00:00+09:00", START),
        ("published", "invalid", None),
        ("published", "2026-11-16T00:00:00", None),
    ],
)
def test_publication_date_formats(tag, value, expected):
    xml = (
        f'<feed><entry><title>A</title><link href="https://example.com/a"/>'
        f"<{tag}>{value}</{tag}></entry></feed>"
    ).encode()
    assert parse_feed(xml)[0].published_at == expected


def test_launch_boundary_unknown_dates_future_and_global_order():
    feeds = {
        "first": feed(
            ("old", "2026-11-15T23:59:59+09:00"),
            ("late", "2026-11-16T10:00:00+09:00"),
            ("unknown", None),
            ("future", "2027-01-01T00:00:00Z"),
        ),
        "second": feed(("boundary", "2026-11-16T00:00:00+09:00")),
    }
    sent = []
    result = run(
        list(feeds),
        feeds.__getitem__,
        lambda a: a,
        sent.append,
        DeliveryState(),
        "2026-11",
        lambda s: None,
        start_at=START,
        now=NOW,
        daily_limit=1,
    )
    assert [a.url for a in result.articles] == ["https://example.com/boundary"]
    assert result.skipped_undated == 1
    assert result.remaining == 1
    assert result.failed_feeds == 0
    assert "失敗" not in result.status
    assert sent[-1].is_summary


def test_partial_send_success_survives_restart():
    xml = feed(("a", START.isoformat()), ("b", START.isoformat()))
    saved = []
    state = DeliveryState()

    def send(digest):
        if digest.articles and digest.articles[0].url.endswith("/b"):
            raise OSError("private webhook details")

    with pytest.raises(OSError):
        run(
            ["x"],
            lambda _: xml,
            lambda a: a,
            send,
            state,
            "2026-11",
            lambda s: saved.append(set(s.delivered_urls)),
            start_at=START,
            now=NOW,
        )
    assert saved[-1] == {"https://example.com/a"}
    retry = run(
        ["x"],
        lambda _: xml,
        lambda a: a,
        lambda d: None,
        DeliveryState(delivered_urls=saved[-1]),
        "2026-11",
        lambda s: None,
        start_at=START,
        now=NOW,
    )
    assert [a.url for a in retry.articles] == ["https://example.com/b"]


def test_summary_failure_does_not_revert_delivered_article():
    state = DeliveryState()

    def send(digest):
        if digest.is_summary:
            raise OSError("summary failed")

    with pytest.raises(OSError):
        run(
            ["x"],
            lambda _: feed(("a", START.isoformat())),
            lambda a: a,
            send,
            state,
            "2026-11",
            lambda s: None,
            start_at=START,
            now=NOW,
        )
    assert state.delivered_urls == {"https://example.com/a"}


def test_naive_start_date_rejected_before_network():
    with pytest.raises(ValueError):
        run(
            ["x"],
            lambda _: pytest.fail("network"),
            lambda a: a,
            lambda d: None,
            DeliveryState(),
            "2026-11",
            lambda s: None,
            start_at=datetime(2026, 11, 16),
        )


def test_published_date_wins_over_recent_update():
    xml = b'<feed><entry><link href="https://example.com/a"/><published>2020-01-01T00:00:00Z</published><updated>2026-11-16T00:00:00Z</updated></entry></feed>'
    result = run(
        ["x"],
        lambda _: xml,
        lambda a: a,
        lambda d: None,
        DeliveryState(),
        "2026-11",
        lambda s: None,
        start_at=START,
        now=NOW,
    )
    assert result.articles == ()


def test_monthly_cap_is_not_reported_as_failure():
    xml = feed(("a", START.isoformat())).replace("日本語a".encode(), b"English")
    result = run(
        ["x"],
        lambda _: xml,
        lambda a: pytest.fail("translation"),
        lambda d: None,
        DeliveryState(month="2026-11", translation_attempts=30),
        "2026-11",
        lambda s: None,
        start_at=START,
        now=NOW,
    )
    assert result.translation_limited
    assert result.translation_failures == 0
    assert result.status == "配信完了（上限あり）"
