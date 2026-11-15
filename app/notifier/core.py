"""収集から配信・既読保存までの順序を管理する。外部SDKには依存しない。"""

from collections.abc import Callable, Sequence

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
) -> Digest:
    """1回の日次配信。同じ状態ファイルを使うプロセスは同時起動しない。"""
    if not 1 <= len(feeds) <= 9:
        raise ValueError("Configure between one and nine feeds")
    if not 1 <= daily_limit <= 10 or not 0 <= monthly_limit <= 30:
        raise ValueError("Limits exceed the supported range")

    if state.month != month:
        state.month = month
        state.translation_attempts = 0

    candidates, notices, failed_feeds = _collect_articles(feeds, fetch, state.delivered_urls)
    selected = candidates[:daily_limit]
    translated = [
        _translate_or_original(article, translate, state, save_state, monthly_limit, notices)
        for article in selected
    ]
    remaining = len(candidates) - len(selected)
    if remaining:
        notices.append(f"日次上限: 未配信{remaining}件（次回のRSSに残っていれば再取得）")

    digest = Digest(tuple(translated), tuple(dict.fromkeys(notices)), failed_feeds, len(feeds))
    send(digest)

    # Slack送信に失敗した場合はここに到達せず、次回も配信候補に残します。
    state.delivered_urls.update(article.url for article in selected)
    save_state(state)
    return digest
