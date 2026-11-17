import pytest
from defusedxml.common import DefusedXmlException

from notifier.feed_parser import is_article_url, parse_feed


def test_atom_extracts_summary_and_alternate_link_not_content():
    xml = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <title>Update</title><link rel="self" href="https://example.com/api"/>
    <link href="https://example.com/article"/><summary>Short summary</summary>
    <content>Full article text</content></entry></feed>"""
    article = parse_feed(xml)[0]
    assert article.url == "https://example.com/article"
    assert article.summary == "Short summary"


def test_rss_strips_html_and_bounds_text():
    xml = b"""<rss><channel><item><title><![CDATA[<b>Title</b>]]></title>
    <link>https://example.com/a</link>
    <description><![CDATA[<p>Summary &amp; more</p>]]></description>
    </item></channel></rss>"""
    article = parse_feed(xml)[0]
    assert article.title == "Title"
    assert article.summary == "Summary & more"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_rejects_dtd_for_multiple_encodings(encoding):
    xml = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE rss><rss/>'
    with pytest.raises(DefusedXmlException):
        parse_feed(xml.encode(encoding))


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:password@example.com",
        "https://example.com/\n",
        "https://[broken",
        "https://example.com/" + "x" * 2000,
    ],
)
def test_rejects_invalid_article_urls(url):
    assert not is_article_url(url)


def test_rejects_oversized_response():
    with pytest.raises(ValueError):
        parse_feed(b"x" * 4_000_001)


def test_large_feed_with_full_descriptions_still_only_keeps_short_summary():
    xml = (
        b"<rss><channel><item><title>A</title><link>https://example.com/a</link>"
        b"<description>" + b"x" * 2_400_000 + b"</description></item></channel></rss>"
    )
    article = parse_feed(xml)[0]
    assert len(article.summary) == 500
