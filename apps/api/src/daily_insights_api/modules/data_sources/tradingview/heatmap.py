"""TradingView stock heatmap capture.

Ported from the Node prototype ``scripts/capture_hsi_heatmap.mjs`` (2026-10-08).
Three things about the page are not documented and were found by trial:

1. The tiles and sector titles are drawn on a ``<canvas>``; only the colour
   legend (``[class*="legend-"]``) and the "All • Sector" breadcrumb are DOM.
2. ``activeGroup`` in the URL hash drills into one sector, but a hash-only
   navigation is ignored by the app. The page must be unloaded (``about:blank``)
   and fully reloaded, and the value must keep the original casing that the
   scanner response uses (``"Technology Services"``).
3. The sector list is not hard-coded: it is aggregated from the page's own POST
   to ``scanner.tradingview.com`` using the ``sector`` and ``market_cap_basic``
   columns.
"""

import asyncio
import hashlib
import json
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from daily_insights_api.modules.data_sources.errors import (
    DataSourceContractError,
    DataSourceTransientError,
)

HEATMAP_PAGE_URL = "https://www.tradingview.com/heatmap/stock/"
SCANNER_URL_PREFIX = "https://scanner.tradingview.com/"
VIEWPORT_WIDTH, VIEWPORT_HEIGHT = 1920, 1200
# The orchestration worker runs as ``nobody`` without user namespaces and with
# a small ``/dev/shm``; both flags are harmless on a developer machine.
CHROMIUM_ARGS = ("--no-sandbox", "--disable-dev-shm-usage")
LEGEND_SELECTOR = '[class*="legend-"]'
MIN_CANVAS_WIDTH = 100
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

HeatmapImageKind = Literal["overview", "legend", "sector"]


@dataclass(frozen=True, slots=True)
class SectorSummary:
    sector: str
    symbol_count: int
    market_cap: float


@dataclass(frozen=True, slots=True)
class HeatmapImage:
    kind: HeatmapImageKind
    slug: str
    name: str
    symbol_count: int
    market_cap: float | None
    sort_order: int
    png: bytes
    width: int
    height: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CapturedHeatmap:
    data_source: str
    captured_at: datetime
    images: tuple[HeatmapImage, ...]


def heatmap_url(data_source: str, active_group: str | None = None) -> str:
    state: dict[str, str] = {
        "dataSource": data_source,
        "blockColor": "change",
        "blockSize": "market_cap_basic",
        "grouping": "sector",
    }
    if active_group is not None:
        state["activeGroup"] = active_group
    return f"{HEATMAP_PAGE_URL}#{quote(json.dumps(state, separators=(',', ':')), safe='')}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def sectors_from_scanner(
    columns: Sequence[str], rows: Sequence[dict[str, Any]]
) -> tuple[SectorSummary, ...]:
    """Aggregate the scanner rows by sector, largest market cap first."""
    try:
        sector_index = columns.index("sector")
        cap_index = columns.index("market_cap_basic")
    except ValueError as error:
        raise DataSourceContractError(
            "scanner request no longer carries sector/market_cap_basic columns"
        ) from error
    counts: dict[str, int] = {}
    caps: dict[str, float] = {}
    for row in rows:
        values = row.get("d")
        if not isinstance(values, list) or len(values) <= max(sector_index, cap_index):
            raise DataSourceContractError("scanner row shorter than its column list")
        sector = values[sector_index]
        if not sector:
            continue
        cap = values[cap_index]
        counts[sector] = counts.get(sector, 0) + 1
        caps[sector] = caps.get(sector, 0.0) + (float(cap) if cap is not None else 0.0)
    summaries = [SectorSummary(name, counts[name], caps[name]) for name in counts]
    summaries.sort(key=lambda item: item.market_cap, reverse=True)
    return tuple(summaries)


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from the IHDR chunk, which always follows the signature."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise DataSourceContractError("screenshot is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _image(
    kind: HeatmapImageKind,
    slug: str,
    name: str,
    png: bytes,
    sort_order: int,
    *,
    symbol_count: int = 0,
    market_cap: float | None = None,
) -> HeatmapImage:
    width, height = png_size(png)
    return HeatmapImage(
        kind=kind,
        slug=slug,
        name=name,
        symbol_count=symbol_count,
        market_cap=market_cap,
        sort_order=sort_order,
        png=png,
        width=width,
        height=height,
        sha256=hashlib.sha256(png).hexdigest(),
    )


def assemble_heatmap(
    data_source: str,
    captured_at: datetime,
    overview_png: bytes,
    legend_png: bytes,
    sector_pngs: Sequence[tuple[SectorSummary, bytes]],
) -> CapturedHeatmap:
    images = [
        _image("overview", "overview", "Overview", overview_png, 0),
        _image("legend", "legend", "Legend", legend_png, 1),
    ]
    for position, (summary, png) in enumerate(sector_pngs, start=2):
        images.append(
            _image(
                "sector",
                slugify(summary.sector),
                summary.sector,
                png,
                position,
                symbol_count=summary.symbol_count,
                market_cap=summary.market_cap,
            )
        )
    return CapturedHeatmap(data_source, captured_at, tuple(images))


async def capture_sector_heatmap(
    *,
    data_source: str = "HSI",
    timeout_seconds: float = 30.0,
    settle_seconds: float = 3.0,
) -> CapturedHeatmap:
    """Screenshot the overview, the legend and every sector of one heatmap."""
    # Playwright is only needed by the orchestration worker; the API process
    # imports this module through data_sources and must not pay for it.
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import FloatRect, Page, Response, ViewportSize, async_playwright

    timeout_ms = timeout_seconds * 1000

    def is_scan(response: Response) -> bool:
        return response.url.startswith(SCANNER_URL_PREFIX) and response.request.method == "POST"

    async def canvas_clip(page: Page) -> FloatRect:
        rect: FloatRect | None = await page.evaluate(
            """() => {
                const rects = [...document.querySelectorAll("canvas")]
                    .map(c => c.getBoundingClientRect())
                    .sort((a, b) => b.width * b.height - a.width * a.height)
                const r = rects[0]
                return r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null
            }"""
        )
        if rect is None or rect["width"] < MIN_CANVAS_WIDTH:
            raise DataSourceContractError("heatmap canvas not found")
        return rect

    async def settle() -> None:
        # ponytail: fixed delay instead of hooking the canvas renderer; hook
        # requestAnimationFrame quiescence if frames come out blank.
        await asyncio.sleep(settle_seconds)

    captured_at = datetime.now(UTC)
    stage = "overview"
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=list(CHROMIUM_ARGS))
            try:
                page = await browser.new_page(
                    viewport=ViewportSize(width=VIEWPORT_WIDTH, height=VIEWPORT_HEIGHT),
                    locale="en-US",
                )
                async with page.expect_response(is_scan, timeout=timeout_ms) as scan_info:
                    await page.goto(heatmap_url(data_source), wait_until="domcontentloaded")
                scan = await scan_info.value
                request_body = json.loads(scan.request.post_data or "{}")
                columns = request_body.get("columns") or []
                rows = (await scan.json()).get("data") or []
                sectors = sectors_from_scanner(columns, rows)

                await settle()
                overview_png = await page.screenshot(clip=await canvas_clip(page))
                legend = page.locator(LEGEND_SELECTOR).first
                await legend.wait_for(state="visible", timeout=timeout_ms)
                legend_png = await legend.screenshot()

                sector_pngs: list[tuple[SectorSummary, bytes]] = []
                for summary in sectors:
                    stage = summary.sector
                    await page.goto("about:blank")
                    async with page.expect_response(is_scan, timeout=timeout_ms):
                        await page.goto(
                            heatmap_url(data_source, summary.sector),
                            wait_until="domcontentloaded",
                        )
                    breadcrumb = page.get_by_text(
                        re.compile(f"^{re.escape(summary.sector)}$", re.IGNORECASE)
                    ).first
                    await breadcrumb.wait_for(state="visible", timeout=timeout_ms)
                    await settle()
                    sector_pngs.append(
                        (summary, await page.screenshot(clip=await canvas_clip(page)))
                    )
            finally:
                await browser.close()
    except PlaywrightError as error:
        raise DataSourceTransientError(f"heatmap capture failed at {stage}: {error}") from error
    return assemble_heatmap(data_source, captured_at, overview_png, legend_png, sector_pngs)
