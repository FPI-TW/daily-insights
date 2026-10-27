"""Local debugging entry point: write one heatmap capture to a directory.

python -m daily_insights_api.modules.data_sources.tradingview --out DIR [--source HSI]
"""

import argparse
import asyncio
import json
from pathlib import Path

from daily_insights_api.modules.data_sources.tradingview.heatmap import capture_sector_heatmap


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source", default="HSI")
    args = parser.parse_args()

    captured = asyncio.run(capture_sector_heatmap(data_source=args.source))
    args.out.mkdir(parents=True, exist_ok=True)
    for image in captured.images:
        (args.out / f"{image.slug}.png").write_bytes(image.png)
        print(f"ok  {image.slug}.png  {image.width}x{image.height}")
    manifest = {
        "captured_at": captured.captured_at.isoformat(),
        "data_source": captured.data_source,
        "images": [
            {
                "kind": image.kind,
                "slug": image.slug,
                "name": image.name,
                "sort_order": image.sort_order,
                "width": image.width,
                "height": image.height,
                "sha256": image.sha256,
            }
            for image in captured.images
        ],
    }
    (args.out / "sectors.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n{len(captured.images)} images -> {args.out}")


if __name__ == "__main__":
    main()
