"""Azure側の起動口。認証・Blob保存を用意して、共通の配信処理へ渡す。"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import azure.functions as func
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import BlobClient

from notifier.core import run
from notifier.feed_parser import parse_timestamp
from notifier.http_client import HttpClient, validate_https_url
from notifier.notifications.line import LineNotifier
from notifier.notifications.slack import SlackNotifier
from notifier.state_store import AlreadyRunningError, BlobStateStore
from notifier.translation import FoundryTranslator

app = func.FunctionApp()
JST = timezone(timedelta(hours=9))


def run_azure():
    # 開始日時の未設定で過去記事を大量配信しないよう、外部接続前に検証します。
    start_at = parse_timestamp(os.environ.get("DELIVERY_START_AT", ""))
    feeds = json.loads(Path(__file__).with_name("feeds.json").read_text(encoding="utf-8-sig"))
    if not isinstance(feeds, list) or not all(isinstance(url, str) for url in feeds):
        raise ValueError("feeds.json must contain feed URLs")

    blob_url = os.environ["STATE_BLOB_URL"]
    validate_https_url(blob_url, host_suffix=".blob.core.windows.net")
    parts = urlsplit(blob_url)
    if parts.query or len(parts.path.strip("/").split("/")) < 2:
        raise ValueError("Use a Blob URL without a SAS token")

    # Azure上では開発者のCLI認証へフォールバックせず、Functions自身のIDだけを使います。
    with ManagedIdentityCredential() as credential:
        with BlobClient.from_blob_url(blob_url, credential=credential) as blob:
            store = BlobStateStore(blob)
            with store.locked():
                state = store.load()
                http = HttpClient()
                translator = FoundryTranslator(
                    os.environ["FOUNDRY_ENDPOINT"],
                    os.environ["FOUNDRY_DEPLOYMENT"],
                    lambda: (
                        credential.get_token("https://cognitiveservices.azure.com/.default").token
                    ),
                    http,
                )
                slack = SlackNotifier(os.environ["SLACK_WEBHOOK_URL"], http)
                line_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
                line_recipient = os.environ.get("LINE_RECIPIENT_ID", "").strip()
                line = None
                if line_token and line_recipient:
                    line = LineNotifier(line_token, line_recipient, http)
                elif line_token or line_recipient:
                    # トークンだけが設定済みでも、既存Slackの配信は止めません。
                    logging.warning("LINE delivery disabled: LINE configuration is incomplete")

                def translate(article):
                    store.ensure_lock()
                    return translator.translate(article)

                def send(digest):
                    # 排他を失った実行が、遅れてSlackへ投稿することを防ぎます。
                    store.ensure_lock()
                    slack.send(digest)

                def send_line(digest):
                    store.ensure_lock()
                    line.send(digest)

                result = run(
                    feeds,
                    http.get,
                    translate,
                    send,
                    state,
                    datetime.now(JST).strftime("%Y-%m"),
                    store.save,
                    start_at=start_at,
                    send_line=send_line if line is not None else None,
                )
                logging.info(
                    "Azure news delivery completed: articles=%s failed_feeds=%s "
                    "translation_failures=%s remaining=%s skipped_undated=%s",
                    len(result.articles),
                    result.failed_feeds,
                    result.translation_failures,
                    result.remaining,
                    result.skipped_undated,
                )
                return result


@app.function_name(name="daily_news")
# Flex LinuxのタイマーはUTC指定。23:00 UTC = 翌日08:00 JST。
@app.timer_trigger(
    schedule="0 0 23 * * *", arg_name="timer", run_on_startup=False, use_monitor=True
)
def daily_news(timer: func.TimerRequest) -> None:
    try:
        run_azure()
    except AlreadyRunningError:
        logging.warning("Azure news delivery skipped: another execution is running")
    except Exception as error:
        # Azure SDKの例外本文にも接続情報が含まれ得るため、型名だけを記録します。
        logging.error("Azure news delivery failed: %s", type(error).__name__)
        raise RuntimeError(
            "Azure news delivery failed; inspect configuration and permissions"
        ) from None
