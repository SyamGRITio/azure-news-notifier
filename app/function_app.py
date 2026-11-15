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
from notifier.http_client import HttpClient, validate_https_url
from notifier.notifications.slack import SlackNotifier
from notifier.state_store import AlreadyRunningError, BlobStateStore
from notifier.translation import FoundryTranslator

app = func.FunctionApp()
JST = timezone(timedelta(hours=9))


def run_azure():
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

                def translate(article):
                    store.ensure_lock()
                    return translator.translate(article)

                def send(digest):
                    # 排他を失った実行が、遅れてSlackへ投稿することを防ぎます。
                    store.ensure_lock()
                    slack.send(digest)

                result = run(
                    feeds,
                    http.get,
                    translate,
                    send,
                    state,
                    datetime.now(JST).strftime("%Y-%m"),
                    store.save,
                )
                logging.info("Azure news delivery completed: articles=%s", len(result.articles))
                return result


@app.function_name(name="daily_news")
@app.timer_trigger(schedule="0 0 0 * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
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
