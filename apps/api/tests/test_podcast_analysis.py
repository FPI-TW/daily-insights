import pytest

from daily_insights_api.modules.podcasts.analysis import (
    ANALYSIS_PROMPT_VERSION,
    MAX_PROMPT_CHARACTERS,
    PodcastAnalysisError,
    Transcript,
    TranscriptSegment,
    analysis_prompt,
    parse_analysis,
    parse_transcription,
    transcript_blocks,
)


def segment(start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(start=start, end=end, text=text)


def sample_transcript() -> Transcript:
    # Nine 10-second sentences: blocks close once they span 20 seconds, so
    # the transcript becomes five blocks starting at 0, 20, 40, 60 and 80.
    return Transcript(
        language="zh",
        duration=90.0,
        text="…",
        segments=tuple(
            segment(index * 10, index * 10 + 10, f"第 {index} 句") for index in range(9)
        ),
    )


def test_parse_transcription_reads_verbose_json_segments() -> None:
    transcript = parse_transcription(
        {
            "task": "transcribe",
            "language": "chinese",
            "duration": 8.47,
            "text": " 大家早安。 今天的重點。 ",
            "segments": [
                {"id": 0, "start": 0.0, "end": 3.3, "text": " 大家早安。"},
                {"id": 1, "start": 3.3, "end": 8.4, "text": " 今天的重點。"},
                {"id": 2, "start": "bad", "end": 9, "text": "dropped"},
                {"id": 3, "start": 8.4, "end": 8.5, "text": "   "},
            ],
        }
    )
    assert transcript.language == "chinese"
    assert transcript.duration == 8.47
    assert transcript.text == "大家早安。 今天的重點。"
    assert [item.text for item in transcript.segments] == ["大家早安。", "今天的重點。"]
    assert transcript.to_json()["segments"][1] == {"start": 3.3, "end": 8.4, "text": "今天的重點。"}


def test_parse_transcription_rejects_non_transcript_payloads() -> None:
    with pytest.raises(PodcastAnalysisError):
        parse_transcription({"error": "nope"})
    with pytest.raises(PodcastAnalysisError):
        parse_transcription("text")


def test_transcript_blocks_merge_segments_into_twenty_second_blocks() -> None:
    blocks = transcript_blocks(sample_transcript())
    assert [block.start_seconds for block in blocks] == [0, 20, 40, 60, 80]
    assert blocks[0].text == "第 0 句 第 1 句"
    # The trailing sentence forms its own short block rather than being lost.
    assert blocks[4].text == "第 8 句"
    only_text = Transcript(language=None, duration=None, text="whole", segments=())
    assert [(block.start_seconds, block.text) for block in transcript_blocks(only_text)] == [
        (0, "whole")
    ]


def test_analysis_prompt_lists_blocks_within_the_character_budget() -> None:
    blocks = transcript_blocks(sample_transcript())
    prompt = analysis_prompt(blocks, trading_date="2026-09-05", duration_seconds=90)
    assert prompt["prompt_version"] == ANALYSIS_PROMPT_VERSION
    assert prompt["episode"] == {"trading_date": "2026-09-05", "duration_seconds": 90}
    assert [item["block"] for item in prompt["blocks"]] == [0, 1, 2, 3, 4]
    assert prompt["output_contract"]["chapters"][0]["block"] == "integer"
    assert any("block 0" in rule for rule in prompt["rules"])

    huge = Transcript(
        language=None,
        duration=None,
        text="x",
        segments=tuple(segment(index * 30, index * 30 + 30, "字" * 5_000) for index in range(10)),
    )
    listed = analysis_prompt(transcript_blocks(huge), trading_date="d", duration_seconds=None)
    assert sum(len(item["text"]) for item in listed["blocks"]) <= MAX_PROMPT_CHARACTERS
    assert len(listed["blocks"]) < 10


def localized(text: str) -> dict[str, str]:
    return {"zh-hant": text, "zh-hans": text, "en": text}


def test_parse_analysis_maps_blocks_to_chapter_starts() -> None:
    blocks = transcript_blocks(sample_transcript())
    result = parse_analysis(
        {
            "title": localized(" 聯準會 按兵不動 "),
            "summary": localized("摘要。"),
            "chapters": [
                {"block": 1, "title": localized("開場")},  # first chapter is pinned to 0
                {"block": 2, "title": localized("外資")},
                {"block": 2, "title": localized("重複的 block 被略過")},
                {"block": 4, "title": localized("清單")},
            ],
        },
        blocks,
        duration_seconds=90,
    )
    assert [item.title for item in result.metadata.values] == ["聯準會 按兵不動"] * 3
    assert [(c.start_seconds, c.title) for c in result.chapters["en"]] == [
        (0, "開場"),
        (40, "外資"),
        (80, "清單"),
    ]


def test_parse_analysis_rejects_bad_shapes() -> None:
    blocks = transcript_blocks(sample_transcript())
    good_chapters = [
        {"block": 0, "title": localized("A")},
        {"block": 2, "title": localized("B")},
        {"block": 4, "title": localized("C")},
    ]
    with pytest.raises(PodcastAnalysisError, match="missing the en"):
        parse_analysis(
            {
                "title": {"zh-hant": "t", "zh-hans": "t"},
                "summary": localized("s"),
                "chapters": good_chapters,
            },
            blocks,
            duration_seconds=90,
        )
    with pytest.raises(PodcastAnalysisError, match="not in the transcript"):
        parse_analysis(
            {
                "title": localized("t"),
                "summary": localized("s"),
                "chapters": [{"block": 9, "title": localized("A")}],
            },
            blocks,
            duration_seconds=90,
        )
    with pytest.raises(PodcastAnalysisError, match="expected 3-6 chapters"):
        parse_analysis(
            {
                "title": localized("t"),
                "summary": localized("s"),
                "chapters": good_chapters[:1],
            },
            blocks,
            duration_seconds=90,
        )
    with pytest.raises(PodcastAnalysisError, match="inside the audio"):
        parse_analysis(
            {"title": localized("t"), "summary": localized("s"), "chapters": good_chapters},
            blocks,
            duration_seconds=70,
        )
