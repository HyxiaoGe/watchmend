import pytest

from sentinel.scan_errors import normalize


@pytest.mark.parametrize(
    "a,b",
    [
        (
            "[2026-06-18 07:27:35,572: ERROR/ForkPoolWorker-15] "
            "Unexpected error for video 9JPiX1ZJQ_U",
            "[2026-06-18 07:27:30,335: ERROR/ForkPoolWorker-8] "
            "Unexpected error for video hIdvhl8PUwc",
        ),
        (
            "ERROR: retry 42 failed at 0xDEADBEEF for 10.0.0.1",
            "ERROR: retry 99 failed at 0xABCDEF12 for 10.0.0.9",
        ),
        (
            "\x1b[92m06:15:03 - LiteLLM:ERROR\x1b[0m: auth failed",
            "\x1b[92m18:42:59 - LiteLLM:ERROR\x1b[0m: auth failed",
        ),
    ],
)
def test_same_error_class_collapses(a, b):
    assert normalize(a) == normalize(b)


def test_different_error_classes_stay_distinct():
    assert normalize("ERROR: database connection failed") != normalize(
        "ERROR: model request rejected"
    )


def test_semantic_compound_words_are_not_overcollapsed():
    assert normalize("ERROR: validation failed for request_method") != normalize(
        "ERROR: validation failed for request_timeout"
    )
