"""Entry point of the ``browser-egress`` Compose service (CloakBrowser's only way out)."""

import asyncio
import logging
import sys

from daily_insights_api.modules.newsroom.ingestion.egress_proxy import serve


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f"browser egress proxy failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
