"""収集した記事と、配信に使う状態・結果。"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Article:
    title: str
    summary: str
    url: str


@dataclass
class DeliveryState:
    month: str = ""
    translation_attempts: int = 0
    delivered_urls: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Digest:
    articles: tuple[Article, ...]
    notices: tuple[str, ...] = ()
    failed_feeds: int = 0
    total_feeds: int = 0

    @property
    def status(self) -> str:
        if self.total_feeds and self.failed_feeds == self.total_feeds:
            return "収集失敗"
        return "一部失敗・制限あり" if self.notices else "正常"

    @property
    def heading(self) -> str:
        return f"Azure更新: {self.status} / {len(self.articles)}件"
