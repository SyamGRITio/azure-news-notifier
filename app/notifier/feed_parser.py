"""RSS 2.0 / RSS 1.0 / Atomから、タイトル・概要・リンクを取り出す。"""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring

from .models import Article

MAX_FEED_BYTES = 4_000_000
MAX_URL_LENGTH = 2_000
MAX_TITLE_LENGTH = 200
MAX_SUMMARY_LENGTH = 500


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return re.sub(r"\s+", " ", unescape(" ".join(parser.parts))).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: Element, names: tuple[str, ...]) -> str:
    for name in names:
        for child in node:
            if _local_name(child.tag) == name:
                return "".join(child.itertext())
    return ""


def _article_url(node: Element) -> str:
    for child in node:
        if _local_name(child.tag) != "link":
            continue
        if child.get("rel", "alternate") == "alternate":
            return (child.get("href") or child.text or "").strip()
    return ""


def is_article_url(url: str) -> bool:
    if len(url) > MAX_URL_LENGTH or re.search(r"[\s\x00-\x1f\x7f]", url):
        return False
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and not parts.username
            and not parts.password
        )
    except ValueError:
        return False


def parse_timestamp(value: str) -> datetime:
    """タイムゾーン付きの設定日時を読み取る。日付だけの指定は拒否する。"""
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, AttributeError):
        raise ValueError("DELIVERY_START_AT must be an ISO datetime with timezone") from None


def _published_at(node: Element) -> datetime | None:
    # 公開日時を優先します。更新日時への差し替えで過去記事を新着にしません。
    value = _child_text(node, ("published", "pubDate", "date", "updated")).strip()
    if not value:
        return None
    try:
        return parse_timestamp(value)
    except ValueError:
        try:
            result = parsedate_to_datetime(value)
            return result.astimezone(UTC) if result.utcoffset() is not None else None
        except (ValueError, TypeError, OverflowError):
            return None


def parse_feed(data: bytes) -> list[Article]:
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Feed exceeds size limit")

    # バイト列の文字コードにかかわらず、DTD・外部参照・Entityを拒否します。
    root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    if _local_name(root.tag) not in ("rss", "feed", "RDF"):
        raise ValueError("Document is not RSS or Atom")

    articles = []
    for node in root.iter():
        if _local_name(node.tag) not in ("item", "entry"):
            continue
        url = _article_url(node)
        if not is_article_url(url):
            continue
        title = plain_text(_child_text(node, ("title",)))[:MAX_TITLE_LENGTH]
        summary = plain_text(_child_text(node, ("description", "summary")))
        articles.append(
            Article(title or "タイトルなし", summary[:MAX_SUMMARY_LENGTH], url, _published_at(node))
        )
    return articles


def has_japanese(text: str) -> bool:
    """混在言語を含む簡易判定。日本語を含むタイトルは翻訳しない。"""
    return bool(re.search(r"[ぁ-んァ-ヶ一-龥]", text))
