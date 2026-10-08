"""Final direct-upload paths; legacy/import metadata helpers remain unchanged."""

import re
from datetime import date

FINAL_PREFIX = "podcasts/"
FINAL_PATTERN = re.compile(
    r"^podcasts/([0-9]{4})/([0-9]{2})/([0-9]{2})/(zh-hant|zh-hans|en)/"
    r"podcast_([1-9][0-9]*)\.(mp3|mp4)$"
)


def final_audio_key(trading_date: date, locale: str, version: int, mime_type: str) -> str:
    if locale not in {"zh-hant", "zh-hans", "en"} or version < 1:
        raise ValueError("Invalid podcast identity")
    if mime_type not in {"audio/mpeg", "audio/mp4"}:
        raise ValueError("Invalid podcast format")
    extension = "mp3" if mime_type == "audio/mpeg" else "mp4"
    return (
        f"podcasts/{trading_date.year:04d}/{trading_date.month:02d}/{trading_date.day:02d}/"
        f"{locale}/podcast_{version}.{extension}"
    )


def final_audio_date(key: str) -> date | None:
    match = FINAL_PATTERN.fullmatch(key)
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None
