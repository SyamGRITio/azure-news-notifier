"""収集した記事と、配信に使う状態・結果。"""

from dataclasses import dataclass, field
from datetime import datetime


# dataclassは、記事の項目をまとめて持つための仕組みです。
@dataclass(frozen=True)
class Article:
    title: str
    summary: str
    url: str
    published_at: datetime | None = None


@dataclass
class DeliveryState:
    month: str = ""
    translation_attempts: int = 0
    # 次回の配信から除外するURLです。API受付済みと、LINE導入時の引き継ぎを含みます。
    # 端末での受信・既読の記録ではありません。
    delivered_urls: set[str] = field(default_factory=set)
    line_delivered_urls: set[str] | None = None


@dataclass(frozen=True)
class Digest:
    articles: tuple[Article, ...]
    notices: tuple[str, ...] = ()
    failed_feeds: int = 0
    total_feeds: int = 0
    translation_failures: int = 0
    remaining: int = 0
    skipped_undated: int = 0
    translation_limited: bool = False
    delivery_failures: int = 0
    is_summary: bool = False
    test_message: bool = False
    sent_slack: int = 0
    sent_line: int = 0
    line_enabled: bool = False

    @property
    def status(self) -> str:
        if self.total_feeds and self.failed_feeds == self.total_feeds:
            return "収集失敗"
        if self.failed_feeds or self.translation_failures or self.delivery_failures:
            return "一部失敗"
        if self.skipped_undated:
            return "配信完了（日時不明の記事あり）"
        if self.remaining or self.translation_limited:
            return "配信完了（上限あり）"
        return "配信完了"

    @property
    def heading(self) -> str:
        return f"{self.status}：{len(self.articles)}件"
