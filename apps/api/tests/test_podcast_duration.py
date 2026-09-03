import io

from daily_insights_api.modules.podcasts.service import audio_duration_seconds


def synthetic_mp3(frames: int) -> bytes:
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding: 417-byte frames of
    # 26.1 ms each. mutagen derives the length from the constant bitrate.
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)
    return frame * frames


def test_duration_is_measured_from_the_upload_and_the_stream_is_rewound() -> None:
    content = io.BytesIO(synthetic_mp3(400))
    content.read(10)

    assert audio_duration_seconds(content) == 10
    assert content.tell() == 0


def test_unreadable_audio_yields_no_duration() -> None:
    content = io.BytesIO(b"simplified-chinese-podcast")

    assert audio_duration_seconds(content) is None
    assert content.tell() == 0
