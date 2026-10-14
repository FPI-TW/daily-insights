import hashlib
import json
import struct
import zlib
from datetime import UTC, datetime
from urllib.parse import unquote

import pytest

from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.tradingview.heatmap import (
    assemble_heatmap,
    heatmap_url,
    png_size,
    slugify,
)

CAPTURED_AT = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


def test_slugify_turns_sector_names_into_file_safe_keys() -> None:
    assert slugify("Technology Services") == "technology_services"
    assert slugify("  Non-Energy Minerals! ") == "non_energy_minerals"


def test_heatmap_url_carries_the_sector_grouped_state_in_the_hash() -> None:
    url = heatmap_url("HSI")
    page, _, fragment = url.partition("#")
    assert page == "https://www.tradingview.com/heatmap/stock/"
    assert json.loads(unquote(fragment)) == {
        "dataSource": "HSI",
        "blockColor": "change",
        "blockSize": "market_cap_basic",
        "grouping": "sector",
    }


def test_png_size_reads_the_ihdr_header() -> None:
    assert png_size(_png(1920, 1080)) == (1920, 1080)
    with pytest.raises(DataSourceContractError):
        png_size(b"not a png")


def test_assemble_heatmap_orders_overview_then_legend() -> None:
    overview, legend = _png(1800, 1000), _png(300, 40)
    captured = assemble_heatmap("HSI", CAPTURED_AT, overview, legend)
    assert captured.data_source == "HSI"
    assert captured.captured_at == CAPTURED_AT
    assert [(image.kind, image.slug, image.sort_order) for image in captured.images] == [
        ("overview", "overview", 0),
        ("legend", "legend", 1),
    ]
    assert (captured.images[0].width, captured.images[0].height) == (1800, 1000)
    assert captured.images[0].sha256 == hashlib.sha256(overview).hexdigest()
