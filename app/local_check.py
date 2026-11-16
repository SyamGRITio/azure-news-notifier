"""ローカル確認専用。デモ・RSS確認・1記事接続確認は通常処理から分離します。"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv

from notifier.core import run
from notifier.feed_parser import has_japanese, parse_feed, parse_timestamp
from notifier.http_client import HttpClient, RedirectBlockedError, ResponseTooLargeError
from notifier.models import Article, DeliveryState, Digest
from notifier.notifications.slack import SlackNotifier
from notifier.state_store import JsonStateStore
from notifier.translation import FoundryTranslator


class ConfigurationError(ValueError):
    """秘密の値を含めずに案内できる設定エラー。"""


def load_local_environment(path: Path | None = None) -> None:
    path = path or Path(__file__).resolve().parent / ".env"
    load_dotenv(path, override=False, encoding="utf-8-sig")
    required = ("FOUNDRY_ENDPOINT", "FOUNDRY_DEPLOYMENT", "SLACK_WEBHOOK_URL")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigurationError("app/.envに設定してください: " + ", ".join(missing))
    if not os.environ["SLACK_WEBHOOK_URL"].startswith("https://hooks.slack.com/"):
        raise ConfigurationError("SLACK_WEBHOOK_URLにはSlack Incoming WebhookのURLが必要です。")


@dataclass(frozen=True)
class FeedCheckResult:
    url: str
    article_count: int | None
    detail: str

    @property
    def succeeded(self) -> bool:
        return self.article_count is not None


def check_feeds(feeds: Sequence[str], fetch: Callable[[str], bytes]) -> list[FeedCheckResult]:
    results = []
    for url in feeds:
        try:
            data = fetch(url)
        except RedirectBlockedError:
            results.append(FeedCheckResult(url, None, "別URLへの転送を拒否（公式の移転先を確認）"))
            continue
        except ResponseTooLargeError:
            results.append(FeedCheckResult(url, None, "応答が4MB上限を超過"))
            continue
        except HTTPError as error:
            results.append(FeedCheckResult(url, None, f"HTTP {error.code}"))
            continue
        except URLError:
            results.append(FeedCheckResult(url, None, "接続失敗（ネットワーク・DNS・TLSを確認）"))
            continue
        except Exception as error:
            results.append(FeedCheckResult(url, None, f"取得失敗（{type(error).__name__}）"))
            continue

        try:
            articles = parse_feed(data)
        except Exception as error:
            results.append(
                FeedCheckResult(url, None, f"XML・フィード解析失敗（{type(error).__name__}）")
            )
            continue
        detail = "取得・解析成功" if articles else "取得・解析成功、記事0件"
        results.append(FeedCheckResult(url, len(articles), detail))
    return results


class SmokeTestError(RuntimeError):
    """接続確認でどの段階が失敗したかを伝える。秘密の値は含めない。"""


def run_smoke_test(
    feeds: Sequence[str],
    fetch: Callable[[str], bytes],
    translate: Callable[[Article], Article],
    send: Callable[[Digest], None],
    state: DeliveryState,
    month: str,
    save_state: Callable[[DeliveryState], None],
) -> Digest:
    """通常配信と違い、翻訳できなければ送らず失敗終了する。"""
    selected = None
    for url in feeds:
        try:
            articles = parse_feed(fetch(url))
        except Exception:
            continue
        selected = next(
            (
                article
                for article in articles
                if not has_japanese(article.title) and article.url not in state.delivered_urls
            ),
            None,
        )
        if selected is not None:
            break
    if selected is None:
        raise SmokeTestError(
            "未配信の英語記事を取得できませんでした。先に--check-feedsでRSSを確認してください。"
        )

    if state.month != month:
        state.month = month
        state.translation_attempts = 0
    if state.translation_attempts >= 30:
        raise SmokeTestError("月30回の翻訳上限に達しています。接続テストも同じ枠を使用します。")

    # 接続テストも実際に課金されるため、通常配信と同じ月間枠を使います。
    state.translation_attempts += 1
    save_state(state)
    try:
        translated = translate(selected)
    except Exception:
        raise SmokeTestError(
            "Foundry翻訳で失敗しました。Azureログイン、呼び出し権限、接続先・モデル名を確認してください。"
            "Slackへは未送信です。"
        ) from None
    if not isinstance(translated, Article) or translated.url != selected.url:
        raise ValueError("Invalid translated article")
    digest = Digest((translated,))
    try:
        send(digest)
    except Exception:
        raise SmokeTestError(
            "Slack送信で失敗しました。Incoming WebhookのURLと通知先を確認してください。"
            "既読にはしていません。"
        ) from None
    state.delivered_urls.add(selected.url)
    save_state(state)
    return digest


JST = timezone(timedelta(hours=9))


DEMO_FEED = b"""<rss><channel><item>
<title>Azure demo</title><link>https://example.com/demo</link>
<description>Local sample. No network requests.</description>
</item></channel></rss>"""


def _print_digest(digest: Digest) -> None:
    if digest.is_summary:
        print(digest.heading)
        for notice in digest.notices:
            print(notice)
        return
    for article in digest.articles:
        print(f"\n{article.title}\n{article.summary}\n{article.url}")
    for notice in digest.notices:
        print(notice)


def _demo_translation(article: Article) -> Article:
    return Article("Azureのサンプル", "外部通信を行わない動作確認です。", article.url)


def _ignore_state(state: DeliveryState) -> None:
    """デモでは状態を保存しない。"""


def _load_feeds(feeds_path: Path) -> list[str]:
    feeds = json.loads(feeds_path.read_text(encoding="utf-8-sig"))
    if not isinstance(feeds, list) or not all(isinstance(url, str) for url in feeds):
        raise ValueError("feeds.json must be a list of URLs")
    if not 1 <= len(feeds) <= 9:
        raise ValueError("Configure between one and nine feeds")
    return feeds


def _check_feeds(feeds_path: Path) -> int:
    results = check_feeds(_load_feeds(feeds_path), HttpClient().get)
    for index, result in enumerate(results, start=1):
        status = "OK" if result.succeeded else "NG"
        count = f" / {result.article_count}記事" if result.succeeded else ""
        print(f"[{status}] feed-{index}: {result.detail}{count}\n  {result.url}")
    succeeded = sum(result.succeeded for result in results)
    print(f"\n取得・解析: {succeeded}/{len(results)}件成功（翻訳・通知・状態保存なし）")
    return 0 if succeeded == len(results) else 1


def _run_live(feeds_path: Path, state_path: Path, month: str, *, smoke_test: bool = False) -> None:
    from azure.identity import DefaultAzureCredential

    load_local_environment()
    feeds = _load_feeds(feeds_path)

    http = HttpClient()
    store = JsonStateStore(state_path)
    state = store.load()
    notifier = SlackNotifier(os.environ["SLACK_WEBHOOK_URL"], http)

    with DefaultAzureCredential() as credential:

        def token_provider() -> str:
            return credential.get_token("https://cognitiveservices.azure.com/.default").token

        translator = FoundryTranslator(
            os.environ["FOUNDRY_ENDPOINT"],
            os.environ["FOUNDRY_DEPLOYMENT"],
            token_provider,
            http,
        )
        if smoke_test:
            run_smoke_test(
                feeds, http.get, translator.translate, notifier.send, state, month, store.save
            )
            print(
                "接続テスト成功: 英語記事1件を翻訳し、Slackへ送信しました。"
                "Slackで内容を確認してください。"
            )
        else:
            run(
                feeds,
                http.get,
                translator.translate,
                notifier.send,
                state,
                month,
                store.save,
                start_at=parse_timestamp(os.environ.get("DELIVERY_START_AT", "")),
            )
            print("配信処理が完了しました。Slackで結果を確認してください。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Azureニュースの日次配信（既定は通信なしデモ）")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="実RSS・Foundry・Slackへ接続する")
    modes.add_argument("--check-feeds", action="store_true", help="RSSのみ実取得・解析して確認する")
    modes.add_argument(
        "--smoke-test", action="store_true", help="実RSSの英語記事1件を翻訳してSlackへ送る"
    )
    parser.add_argument(
        "--feeds", type=Path, default=Path(__file__).resolve().parent / "feeds.json"
    )
    parser.add_argument("--state", type=Path, default=Path(".notifier-state.json"))
    args = parser.parse_args()
    month = datetime.now(JST).strftime("%Y-%m")

    try:
        if args.check_feeds:
            return _check_feeds(args.feeds)
        if args.smoke_test:
            _run_live(args.feeds, args.state, month, smoke_test=True)
        elif args.live:
            _run_live(args.feeds, args.state, month)
        else:
            run(
                ["demo"],
                lambda _: DEMO_FEED,
                _demo_translation,
                _print_digest,
                DeliveryState(),
                month,
                _ignore_state,
            )
    except (ConfigurationError, SmokeTestError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # HTTP例外にはWebhook等が含まれ得るため、CLIでは例外本文を出しません。
        print(
            "配信を完了できませんでした。設定・認証・通信・状態ファイルを確認してください。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
