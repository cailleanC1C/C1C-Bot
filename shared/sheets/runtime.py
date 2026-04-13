"""Runtime helpers for coordinating Sheets package setup."""

from __future__ import annotations

__all__ = ["register_default_cache_buckets"]


def register_default_cache_buckets() -> None:
    """Register cache buckets required by Sheets modules."""

    import shared.sheets.onboarding as onboarding
    import shared.sheets.onboarding_questions as onboarding_questions
    import shared.sheets.config_service as config_service
    import shared.sheets.fusion as fusion
    import shared.sheets.recruitment as recruitment
    import shared.sheets.reaction_roles as reaction_roles

    onboarding.register_cache_buckets()
    onboarding_questions.register_cache_buckets()
    config_service.register_cache_buckets()
    fusion.register_cache_buckets()
    recruitment.register_cache_buckets()
    reaction_roles.register_cache_buckets()
