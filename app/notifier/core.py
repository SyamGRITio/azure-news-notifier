"""収集から配信・既読保存までの順序を管理する。外部SDKには依存しない。"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal

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
) -> tuple[Article, Literal["failed", "limited"] | None]:
    # 記事と結果区分を返します。通知文を変えても失敗件数の集計が壊れない形です。
    if has_japanese(article.title):
        return article, None
    if state.translation_attempts >= monthly_limit:
        return article, "limited"

    # API呼び出し前に枠を予約保存します。保存失敗時は翻訳せず処理を停止します。
    state.translation_attempts += 1
    save_state(state)
    try:
        translated = translate(article)
        if not isinstance(translated, Article) or translated.url != article.url:
            raise ValueError("Translator changed article identity")
        return translated, None
    except Exception:
        return article, "failed"


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
    send_line: SendDigest | None = None,
) -> Digest:
    """日次配信の本体。ここを上から読むと通常処理の順序を追えます。

    設定確認 → RSS収集 → 新着・未送信を選択 → 翻訳 → 各宛先へ送信・保存 → 結果通知。
    Azure認証やBlobの排他は、呼び出し元のfunction_app.pyが用意します。
    """
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

    if send_line is not None and state.line_delivered_urls is None:
        # LINEの初回追加で過去のSlack配信記事をまとめて再送しないよう引き継ぎます。
        state.line_delivered_urls = set(state.delivered_urls)
        save_state(state)
    delivered_urls = state.delivered_urls
    if send_line is not None:
        delivered_urls = state.delivered_urls & state.line_delivered_urls
    # 収集と選別: サイトから記事を取り、開始日時と送信履歴で対象を絞ります。
    candidates, notices, failed_feeds = _collect_articles(feeds, fetch, delivered_urls)
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
    sent_slack = 0
    sent_line = 0
    send_errors: list[Exception] = []

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
            sent_slack=sent_slack,
            sent_line=sent_line,
            line_enabled=send_line is not None,
        )

    # 記事ごとの処理: 翻訳は共通で行い、配信と保存は宛先ごとに行います。
    for article in selected:
        translated, translation_status = _translate_or_original(
            article, translate, state, save_state, monthly_limit
        )
        if translation_status == "failed":
            translation_failures += 1
            notices.append("翻訳失敗: 原文を配信")
        elif translation_status == "limited":
            translation_limited = True
            notices.append("月間翻訳上限: 原文を配信")
        # 送信先ごとに成功を保存します。片方の失敗でも、もう片方は配信を続けます。
        destinations = [("Slack", send, state.delivered_urls)]
        if send_line is not None:
            destinations.append(("LINE", send_line, state.line_delivered_urls))
        for name, notify, successful_urls in destinations:
            if article.url in successful_urls:
                continue
            try:
                notify(Digest((translated,)))
            except Exception as error:
                send_errors.append(error)
                notices.append(f"{name}送信失敗: 未送信の記事は次回再試行")
                continue
            successful_urls.add(article.url)
            save_state(state)
            if name == "Slack":
                sent_slack += 1
            else:
                sent_line += 1
        if article.url in state.delivered_urls or (
            send_line is not None and article.url in state.line_delivered_urls
        ):
            delivered.append(translated)

    # 利用者向けの結果はSlackへ1回だけ。詳細件数は呼び出し元でログに残します。
    digest = result(delivery_failures=len(send_errors))
    send(digest)
    save_state(state)
    if send_errors:
        raise send_errors[0]
    return digest
