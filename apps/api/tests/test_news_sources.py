from datetime import UTC, datetime

import httpx
import pytest

from daily_insights_api.modules.news.sources import (
    _ArticleTextExtractor,
    configured_hostnames,
    discover_candidates,
    fetch_article,
    validate_https_url,
)

ALLOWED = configured_hostnames("www.reuters.com,apnews.com")


async def test_gdelt_filters_window_schema_exact_host_and_deduplicates() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "https://www.reuters.com/markets/a",
                        "title": "Market rises",
                        "seendate": "20260901100000",
                    },
                    {
                        "url": "https://www.reuters.com/markets/a",
                        "title": "Market rises",
                        "seendate": "20260901100000",
                    },
                    {
                        "url": "https://reuters.com/markets/b",
                        "title": "Rejected hostname",
                        "seendate": "20260901100000",
                    },
                    {"url": "https://apnews.com/b", "title": "Missing timestamp"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await discover_candidates(client, ALLOWED, datetime(2026, 9, 1, 11, tzinfo=UTC))
    assert [item.headline for item in result] == ["Market rises"]


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
    import daily_insights_api.modules.news.sources as sources

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
    import daily_insights_api.modules.news.sources as sources

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


async def test_gdelt_schema_failure_is_rejected() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"articles": "not-a-list"})

    with pytest.raises(ValueError, match="articles"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await discover_candidates(client, ALLOWED, datetime(2026, 9, 1, tzinfo=UTC))
