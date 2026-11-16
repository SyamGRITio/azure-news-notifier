"""収集から配信・既読保存までの順序を管理する。外部SDKには依存しない。"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from .feed_parser import has_japanese, parse_feed
from .models import Article, DeliveryState, Digest

FetchFeed = Callable[[str], bytes]
TranslateArticle = Callable[[Article], Article]
SendDigest = Callable[[Digest], None]
SaveState = Callable[[DeliveryState], None]


def _collect_articles(
    feeds: Sequence[str], fetch: FetchFeed, delivered_urls: set[str]
) -> tuple[list[Article], list[str], int]:
    articles: list[Article] = []
    notices: list[str] = []
    known_urls = set(delivered_urls)
    failed_feeds = 0

    for index, feed_url in enumerate(feeds, start=1):
        try:
            entries = parse_feed(fetch(feed_url))
        except Exception:
            # 配信元ごとに失敗を分離し、例外に含まれ得る秘密情報は通知しません。
            failed_feeds += 1
            notices.append(f"RSS取得失敗: feed-{index}")
            continue

        for article in entries:
            if article.url not in known_urls:
                articles.append(article)
                known_urls.add(article.url)

    return articles, notices, failed_feeds


def _translate_or_original(
    article: Article,
    translate: TranslateArticle,
    state: DeliveryState,
    save_state: SaveState,
    monthly_limit: int,
    notices: list[str],
) -> Article:
    if has_japanese(article.title):
        return article
    if state.translation_attempts >= monthly_limit:
        notices.append("月間翻訳上限: 原文を配信")
        return article

    # API呼び出し前に枠を予約保存します。保存失敗時は翻訳せず処理を停止します。
    state.translation_attempts += 1
    save_state(state)
    try:
        translated = translate(article)
        if not isinstance(translated, Article) or translated.url != article.url:
            raise ValueError("Translator changed article identity")
        return translated
    except Exception:
        notices.append("翻訳失敗: 原文を配信")
        return article


def run(
    feeds: Sequence[str],
    fetch: FetchFeed,
    translate: TranslateArticle,
    send: SendDigest,
    state: DeliveryState,
    month: str,
    save_state: SaveState,
    *,
    daily_limit: int = 10,
    monthly_limit: int = 30,
    start_at: datetime | None = None,
    now: datetime | None = None,
) -> Digest:
    """1回の日次配信。同じ状態ファイルを使うプロセスは同時起動しない。"""
    if not 1 <= len(feeds) <= 9:
        raise ValueError("Configure between one and nine feeds")
    if not 1 <= daily_limit <= 10 or not 0 <= monthly_limit <= 30:
        raise ValueError("Limits exceed the supported range")

    if state.month != month:
        state.month = month
        state.translation_attempts = 0

    if start_at is not None and start_at.utcoffset() is None:
        raise ValueError("start_at must have a timezone")
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("now must have a timezone")

    candidates, notices, failed_feeds = _collect_articles(feeds, fetch, state.delivered_urls)
    skipped_undated = 0
    if start_at is not None:
        skipped_undated = sum(article.published_at is None for article in candidates)
        candidates = [
            article
            for article in candidates
            if article.published_at is not None and start_at <= article.published_at <= now
        ]
        # 登録順で最初のRSSが枠を独占しないよう、全配信元を公開日時順に並べます。
        candidates.sort(key=lambda article: article.published_at)
    selected = candidates[:daily_limit]
    remaining = len(candidates) - len(selected)
    if remaining:
        notices.append(f"件数上限: 未配信{remaining}件（次回のRSSに残っていれば再取得）")
    if skipped_undated:
        notices.append(f"日時不明: {skipped_undated}件を除外")

    delivered: list[Article] = []
    translation_failures = 0
    translation_limited = False

    def result(*, delivery_failures: int = 0) -> Digest:
        return Digest(
            tuple(delivered),
            tuple(dict.fromkeys(notices)),
            failed_feeds,
            len(feeds),
            translation_failures=translation_failures,
            remaining=remaining,
            skipped_undated=skipped_undated,
            translation_limited=translation_limited,
            delivery_failures=delivery_failures,
            is_summary=True,
        )

    for article in selected:
        article_notices: list[str] = []
        translated = _translate_or_original(
            article, translate, state, save_state, monthly_limit, article_notices
        )
        translation_failures += int("翻訳失敗: 原文を配信" in article_notices)
        translation_limited |= "月間翻訳上限: 原文を配信" in article_notices
        notices.extend(article_notices)
        try:
            send(Digest((translated,)))
        except Exception:
            # 先に送れた記事の既読は残し、未送信の記事だけ次回の候補にします。
            try:
                send(result(delivery_failures=1))
            except Exception:
                pass  # 通知先自体が停止している場合は、呼び出し元の失敗ログで確認します。
            raise
        state.delivered_urls.add(article.url)
        save_state(state)
        delivered.append(translated)

    digest = result()
    send(digest)
    save_state(state)
    return digest
