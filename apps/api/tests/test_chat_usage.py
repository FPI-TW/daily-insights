import pytest

from daily_insights_api.modules.chat.usage import token_usage_summary


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "generations", "inputs", "outputs", "total", "completeness"),
    [
        (12, 34, 1, 1, 1, 46, "complete"),
        (0, 0, 1, 1, 1, 0, "complete"),
        (12, None, 2, 1, 0, 12, "partial"),
        (None, 0, 1, 0, 1, 0, "partial"),
        (12, 34, 2, 1, 1, 46, "partial"),
        (None, None, 2, 0, 0, None, "unavailable"),
        (None, None, 0, 0, 0, None, "unavailable"),
    ],
)
def test_usage_completeness(
    input_tokens: int | None,
    output_tokens: int | None,
    generations: int,
    inputs: int,
    outputs: int,
    total: int | None,
    completeness: str,
) -> None:
    summary = token_usage_summary(input_tokens, output_tokens, generations, inputs, outputs)
    assert summary.input_tokens == input_tokens
    assert summary.output_tokens == output_tokens
    assert summary.total_tokens == total
    assert summary.completeness == completeness
