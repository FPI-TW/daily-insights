import io

import pytest
from mutagen.id3 import CHAP, CTOC, ID3, TIT2, CTOCFlags
from pydantic import ValidationError

from daily_insights_api.modules.podcasts.api import (
    PodcastChapter,
    PodcastChaptersUpdate,
    validate_chapters,
)
from daily_insights_api.modules.podcasts.service import audio_chapters, parsed_chapters


def id3_with_chapters(markers: list[tuple[int, int, str]]) -> io.BytesIO:
    # mutagen ships no type hints for its frame constructors.
    tags = ID3()  # type: ignore[no-untyped-call]
    ids = []
    for index, (start_ms, end_ms, title) in enumerate(markers):
        element_id = f"ch{index}"
        ids.append(element_id)
        title_frame = TIT2(encoding=3, text=[title])  # type: ignore[no-untyped-call]
        chapter = CHAP(  # type: ignore[no-untyped-call]
            element_id=element_id,
            start_time=start_ms,
            end_time=end_ms,
            sub_frames=[title_frame],
        )
        tags.add(chapter)  # type: ignore[no-untyped-call]
    toc = CTOC(  # type: ignore[no-untyped-call]
        element_id="toc",
        flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
        child_element_ids=ids,
        sub_frames=[],
    )
    tags.add(toc)  # type: ignore[no-untyped-call]
    content = io.BytesIO()
    tags.save(content)
    content.seek(0)
    return content


def test_audio_chapters_reads_id3_chap_frames_in_start_order() -> None:
    content = id3_with_chapters(
        [
            (130_000, 285_000, "外資回補三大權值股"),
            (0, 130_000, "FOMC 決議"),
            (285_000, 410_000, "  "),  # untitled markers are dropped
            (410_000, 490_000, "今日觀察清單"),
        ]
    )
    chapters = audio_chapters(content, "audio/mpeg")
    assert [(item.start_seconds, item.title) for item in chapters] == [
        (0, "FOMC 決議"),
        (130, "外資回補三大權值股"),
        (410, "今日觀察清單"),
    ]
    # The stream is rewound so the upload can still be stored.
    assert content.tell() == 0


def test_audio_chapters_is_empty_for_unreadable_or_untagged_files() -> None:
    content = io.BytesIO(b"not really audio")
    assert audio_chapters(content, "audio/mpeg") == ()
    assert audio_chapters(content, "audio/mp4") == ()
    assert content.tell() == 0


def test_validate_chapters_requires_ascending_starts_inside_the_audio() -> None:
    chapters = (
        PodcastChapter(start_seconds=0, title="A"),
        PodcastChapter(start_seconds=130, title="B"),
    )
    assert validate_chapters(chapters, duration_seconds=490) == chapters
    with pytest.raises(ValueError, match="ascending"):
        validate_chapters((chapters[1], chapters[0]))
    with pytest.raises(ValueError, match="ascending"):
        validate_chapters((chapters[0], chapters[0]))
    with pytest.raises(ValueError, match="inside"):
        validate_chapters(chapters, duration_seconds=130)
    with pytest.raises(ValueError, match="at most"):
        validate_chapters(
            tuple(PodcastChapter(start_seconds=index, title=str(index)) for index in range(21))
        )


def test_chapters_update_contract_rejects_disorder_and_empty_titles() -> None:
    PodcastChaptersUpdate(
        expected_version=3,
        chapters=(PodcastChapter(start_seconds=0, title="A"),),
        reason="fix marker",
    )
    with pytest.raises(ValidationError):
        PodcastChaptersUpdate.model_validate(
            {
                "expected_version": 3,
                "chapters": [
                    {"start_seconds": 10, "title": "B"},
                    {"start_seconds": 5, "title": "A"},
                ],
                "reason": "fix marker",
            }
        )
    with pytest.raises(ValidationError):
        PodcastChaptersUpdate.model_validate(
            {
                "expected_version": 3,
                "chapters": [{"start_seconds": 0, "title": ""}],
                "reason": "fix marker",
            }
        )


def test_parsed_chapters_tolerates_malformed_storage() -> None:
    assert parsed_chapters([{"start_seconds": 0, "title": "A"}]) == (
        PodcastChapter(start_seconds=0, title="A"),
    )
    assert parsed_chapters("nope") == ()
    assert parsed_chapters([{"start_seconds": -1, "title": "A"}]) == ()
