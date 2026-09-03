import hashlib
from datetime import UTC, datetime

import httpx
import pytest

from daily_insights_api.modules.news.extraction import (
    _ArticleTextExtractor,
    configured_hostnames,
    fetch_article,
    validate_https_url,
)

ALLOWED = configured_hostnames("www.reuters.com,apnews.com")


def test_extraction_prioritizes_article_and_removes_navigation() -> None:
    parser = _ArticleTextExtractor()
    parser.feed("<nav>ignore navigation</nav><main>fallback</main><article>facts 123</article>")
    assert parser.text() == "facts 123"


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("2026-09-01T08:30:00", None),
        ("not-a-date", None),
        ("2026-09-01T08:30:00Z", datetime(2026, 9, 1, 8, 30, tzinfo=UTC)),
        ("2026-09-01T16:30:00+08:00", datetime(2026, 9, 1, 8, 30, tzinfo=UTC)),
    ],
)
def test_article_published_time_requires_timezone_and_normalizes_utc(
    published: str, expected: datetime | None
) -> None:
    parser = _ArticleTextExtractor()
    parser.feed(f'<meta property="article:published_time" content="{published}">')
    assert parser.published_at == expected


def test_url_validation_rejects_non_standard_https_ports() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        validate_https_url("https://www.reuters.com:8443/article", ALLOWED)


async def test_fetch_revalidates_redirect_and_rejects_unapproved_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daily_insights_api.modules.news.extraction as sources

    async def public(_: str, __: frozenset[str]) -> None:
        return None

    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    monkeypatch.setattr(sources, "assert_public_hostname", public)
    monkeypatch.setattr(sources, "robots_allowed", robots)

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/article"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="allowlisted"):
            await fetch_article(client, "https://www.reuters.com/article", ALLOWED)


async def test_fetch_rejects_wrong_content_type_and_short_paywall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import daily_insights_api.modules.news.extraction as sources

    async def public(_: str, __: frozenset[str]) -> None:
        return None

    async def robots(_: httpx.AsyncClient, __: str, ___: frozenset[str]) -> bool:
        return True

    monkeypatch.setattr(sources, "assert_public_hostname", public)
    monkeypatch.setattr(sources, "robots_allowed", robots)

    async def wrong(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"pdf", headers={"content-type": "application/pdf"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong)) as client:
        with pytest.raises(ValueError, match="content type"):
            await fetch_article(client, "https://www.reuters.com/article", ALLOWED)

    async def paywall(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<article>Subscribe</article>", headers={"content-type": "text/html"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(paywall)) as client:
        with pytest.raises(ValueError, match="short"):
            await fetch_article(client, "https://www.reuters.com/article", ALLOWED)


async def test_feed_supplied_bodies_skip_article_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    from daily_insights_api.modules.news import service
    from daily_insights_api.modules.news.contracts import Candidate

    fetched_urls: list[str] = []

    async def fetch_article(*args: object, **kwargs: object) -> tuple[str, str, None]:
        del kwargs
        fetched_urls.append(str(args[1]))
        return str(args[1]), "Fetched article body", None

    monkeypatch.setattr(service, "fetch_article", fetch_article)
    full = Candidate(
        id="a" * 64,
        url="https://news.cnyes.com/news/id/1",
        hostname="news.cnyes.com",
        source_name="cnyes",
        headline="Full text",
        seen_at=datetime(2026, 9, 2, 1, 0, tzinfo=UTC),
    )
    partial = Candidate(
        id="b" * 64,
        url="https://news.cnyes.com/news/id/2",
        hostname="news.cnyes.com",
        source_name="cnyes",
        headline="Needs fetching",
    )
    allowed = configured_hostnames("news.cnyes.com")

    usable = await service._fetch_usable_candidates(
        [full, partial], allowed, 5, {full.id: "Feed body " * 30}
    )

    assert fetched_urls == [str(partial.url)]
    by_id = {item.candidate.id: item for item in usable}
    assert by_id[full.id].body == "Feed body " * 30
    assert by_id[full.id].content_digest == hashlib.sha256(("Feed body " * 30).encode()).hexdigest()
    assert by_id[full.id].source_published_at == full.seen_at
    assert by_id[partial.id].body == "Fetched article body"
