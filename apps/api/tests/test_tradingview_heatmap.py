import hashlib
import io
import json
import struct
import zlib
from datetime import UTC, datetime
from urllib.parse import unquote

import pytest
from PIL import Image

from daily_insights_api.modules.data_sources.errors import DataSourceContractError
from daily_insights_api.modules.data_sources.tradingview.heatmap import (
    SectorSummary,
    assemble_heatmap,
    column_gutters,
    heatmap_url,
    png_size,
    sectors_from_scanner,
    slugify,
    split_overview,
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


def _row(sector: str | None, market_cap: float | None) -> dict[str, object]:
    return {"s": "HKEX:0001", "d": ["0001", sector, market_cap]}


COLUMNS = ("name", "sector", "market_cap_basic")


def test_slugify_turns_sector_names_into_file_safe_keys() -> None:
    assert slugify("Technology Services") == "technology_services"
    assert slugify("  Non-Energy Minerals! ") == "non_energy_minerals"


def test_heatmap_url_keeps_the_active_group_casing_in_the_hash() -> None:
    url = heatmap_url("HSI", "Technology Services")
    page, _, fragment = url.partition("#")
    assert page == "https://www.tradingview.com/heatmap/stock/"
    assert json.loads(unquote(fragment)) == {
        "dataSource": "HSI",
        "blockColor": "change",
        "blockSize": "market_cap_basic",
        "grouping": "sector",
        "activeGroup": "Technology Services",
    }
    assert "activeGroup" not in heatmap_url("HSI")


def test_sectors_are_aggregated_and_ordered_by_market_cap() -> None:
    sectors = sectors_from_scanner(
        COLUMNS,
        [
            _row("Finance", 100.0),
            _row("Technology Services", 500.0),
            _row("Finance", 250.0),
            _row(None, 999.0),
            _row("Utilities", None),
        ],
    )
    assert sectors == (
        SectorSummary("Technology Services", 1, 500.0),
        SectorSummary("Finance", 2, 350.0),
        SectorSummary("Utilities", 1, 0.0),
    )


@pytest.mark.parametrize(
    "columns, rows",
    [
        (("name", "market_cap_basic"), [_row("Finance", 1.0)]),
        (COLUMNS, [{"s": "HKEX:0001", "d": ["0001"]}]),
    ],
)
def test_scanner_contract_drift_raises(
    columns: tuple[str, ...], rows: list[dict[str, object]]
) -> None:
    with pytest.raises(DataSourceContractError):
        sectors_from_scanner(columns, rows)


def test_png_size_reads_the_ihdr_header() -> None:
    assert png_size(_png(1920, 1080)) == (1920, 1080)
    with pytest.raises(DataSourceContractError):
        png_size(b"not a png")


def test_assemble_heatmap_orders_overview_legend_then_sectors() -> None:
    overview, legend, finance = _png(1800, 1000), _png(300, 40), _png(1800, 1000)
    captured = assemble_heatmap(
        "HSI",
        CAPTURED_AT,
        overview,
        legend,
        [(SectorSummary("Finance", 12, 350.0), finance)],
    )
    assert captured.data_source == "HSI"
    assert captured.captured_at == CAPTURED_AT
    assert [(image.kind, image.slug, image.sort_order) for image in captured.images] == [
        ("overview", "overview", 0),
        ("legend", "legend", 1),
        ("sector", "finance", 2),
    ]
    sector = captured.images[2]
    assert (sector.name, sector.symbol_count, sector.market_cap) == ("Finance", 12, 350.0)
    assert (sector.width, sector.height) == (1800, 1000)
    assert sector.sha256 == hashlib.sha256(finance).hexdigest()
    assert captured.images[1].market_cap is None


def _treemap(width: int, gutters: tuple[int, ...], header_rows: int = 12) -> bytes:
    """Grey tiles with white gutters, plus a light header strip across the top."""
    image = Image.new("L", (width, 100), 160)
    for x in range(width):
        for y in range(header_rows):
            image.putpixel((x, y), 240)
    for gutter in gutters:
        for dx in range(4):
            for y in range(100):
                image.putpixel((gutter + dx, y), 255)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_gutters_are_full_height_light_columns_inside_the_search_range() -> None:
    # 20 sits before the 15% search floor and 990 after the 85% ceiling.
    assert column_gutters(_treemap(1000, (20, 375, 602, 990))) == (376, 603)
    assert column_gutters(_treemap(1000, ())) == ()


def test_overview_is_split_at_the_gutter_nearest_sixty_percent() -> None:
    halves = split_overview(_treemap(1000, (375, 602)))
    assert halves is not None
    left, right = halves
    assert png_size(left) == (603, 100)
    assert png_size(right) == (397, 100)
    assert split_overview(_treemap(1000, ())) is None


def test_assemble_heatmap_places_overview_halves_before_the_legend() -> None:
    captured = assemble_heatmap(
        "HSI",
        CAPTURED_AT,
        _png(1000, 100),
        _png(300, 40),
        [(SectorSummary("Finance", 12, 350.0), _png(1000, 100))],
        overview_halves=(_png(600, 100), _png(400, 100)),
    )
    assert [(image.kind, image.slug, image.sort_order) for image in captured.images] == [
        ("overview", "overview", 0),
        ("overview", "overview_left", 1),
        ("overview", "overview_right", 2),
        ("legend", "legend", 3),
        ("sector", "finance", 4),
    ]
    assert captured.images[1].width == 600
