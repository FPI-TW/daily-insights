"""TradingView stock heatmap capture.

Ported from the Node prototype ``scripts/capture_hsi_heatmap.mjs`` (2026-10-08).
Only the whole-market overview and its colour legend are captured: the tiles
and sector titles are drawn on a ``<canvas>`` (the biggest one on the page),
while the legend (``[class*="legend-"]``) is DOM. The page's own POST to
``scanner.tradingview.com`` is awaited as the signal that the data arrived.
"""

import asyncio
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
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
HeatmapImageKind = Literal["overview", "legend"]


@dataclass(frozen=True, slots=True)
class HeatmapImage:
    kind: HeatmapImageKind
    slug: str
    name: str
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


def heatmap_url(data_source: str) -> str:
    state = {
        "dataSource": data_source,
        "blockColor": "change",
        "blockSize": "market_cap_basic",
        "grouping": "sector",
    }
    return f"{HEATMAP_PAGE_URL}#{quote(json.dumps(state, separators=(',', ':')), safe='')}"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from the IHDR chunk, which always follows the signature."""
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise DataSourceContractError("screenshot is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _image(
    kind: HeatmapImageKind, slug: str, name: str, png: bytes, sort_order: int
) -> HeatmapImage:
    width, height = png_size(png)
    return HeatmapImage(
        kind=kind,
        slug=slug,
        name=name,
        sort_order=sort_order,
        png=png,
        width=width,
        height=height,
        sha256=hashlib.sha256(png).hexdigest(),
    )


def assemble_heatmap(
    data_source: str, captured_at: datetime, overview_png: bytes, legend_png: bytes
) -> CapturedHeatmap:
    images = (
        _image("overview", "overview", "Overview", overview_png, 0),
        _image("legend", "legend", "Legend", legend_png, 1),
    )
    return CapturedHeatmap(data_source, captured_at, images)


async def capture_sector_heatmap(
    *,
    data_source: str = "HSI",
    timeout_seconds: float = 30.0,
    settle_seconds: float = 3.0,
) -> CapturedHeatmap:
    """Screenshot one heatmap's overview and colour legend."""
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
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=list(CHROMIUM_ARGS))
            try:
                page = await browser.new_page(
                    viewport=ViewportSize(width=VIEWPORT_WIDTH, height=VIEWPORT_HEIGHT),
                    locale="en-US",
                )
                async with page.expect_response(is_scan, timeout=timeout_ms):
                    await page.goto(heatmap_url(data_source), wait_until="domcontentloaded")
                await settle()
                overview_png = await page.screenshot(clip=await canvas_clip(page))
                legend = page.locator(LEGEND_SELECTOR).first
                await legend.wait_for(state="visible", timeout=timeout_ms)
                legend_png = await legend.screenshot()
            finally:
                await browser.close()
    except PlaywrightError as error:
        raise DataSourceTransientError(f"heatmap capture failed: {error}") from error
    return assemble_heatmap(data_source, captured_at, overview_png, legend_png)
