from shared.logfmt import BucketResult
from shared.obs.events import format_refresh_message


def test_refresh_message_includes_onboarding_metadata() -> None:
    bucket = BucketResult(
        name="onboarding_questions",
        status="ok",
        duration_s=0.5,
        item_count=5,
        ttl_ok=True,
        retries=None,
        reason=None,
        metadata={"sheet": "abcdef", "tab": "OnboardingQuestions"},
    )

    message = format_refresh_message("startup", [bucket], total_s=0.5)

    assert "onboarding_questions ok" in message
    assert "sheet=abcdef" not in message
    assert "tab=OnboardingQuestions" not in message
    assert "0.5s" in message


def test_refresh_message_renders_age_and_ttl_when_available() -> None:
    bucket = BucketResult(
        name="clans",
        status="ok",
        duration_s=1.8,
        item_count=24,
        ttl_ok=False,
        cache_age_s=17 * 60 * 1000,
        ttl_s=5 * 60 * 1000,
    )

    message = format_refresh_message("startup", [bucket], total_s=1.8)

    assert "clans ok" in message
    assert "stale" in message
    assert "age=17m" in message
    assert "ttl=5m" in message


def test_refresh_message_omits_age_and_ttl_when_unavailable() -> None:
    bucket = BucketResult(
        name="custom_bucket",
        status="ok",
        duration_s=0.7,
        item_count=62,
        ttl_ok=None,
        cache_age_s=None,
        ttl_s=None,
    )

    message = format_refresh_message("startup", [bucket], total_s=0.7)

    assert "custom_bucket ok" in message
    assert "age=" not in message
    assert "ttl=" not in message
