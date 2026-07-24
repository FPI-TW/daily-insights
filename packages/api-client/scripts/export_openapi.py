import json
import sys
from pathlib import Path

from daily_insights_api.web.app import create_app


def main() -> None:
    target = Path(sys.argv[1])
    target.write_text(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
