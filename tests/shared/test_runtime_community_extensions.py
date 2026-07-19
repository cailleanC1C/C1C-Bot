import asyncio
from types import SimpleNamespace

from modules.common import feature_flags
from modules.common import runtime


def test_refresh_surfaces_global_feature_toggle_failure(monkeypatch):
    async def refresh():
        return None

    monkeypatch.setattr(feature_flags, "refresh", refresh)
    monkeypatch.setattr(
        feature_flags,
        "status",
        lambda _key: {"global_failure_reason": "global toggle load failed"},
    )

    assert asyncio.run(runtime._refresh_feature_toggles()) == (
        "global toggle load failed"
    )


def test_community_extensions_fail_closed_and_are_added_to_alert(monkeypatch):
    configured = (
        "modules.community.example",
        "modules.community.another",
    )
    monkeypatch.setattr(runtime, "COMMUNITY_EXTENSIONS", configured)
    loaded = []

    async def load_extension(path):
        loaded.append(path)

    skipped = []
    asyncio.run(
        runtime._load_community_extensions(
            SimpleNamespace(load_extension=load_extension),
            failure_reason="global toggle load failed",
            skipped=skipped,
        )
    )

    assert loaded == []
    assert skipped == list(configured)
    alert = f"affected={', '.join(skipped)}"
    assert configured[0] in alert


def test_community_extensions_load_after_successful_toggle_refresh(monkeypatch):
    configured = (
        "modules.community.example",
        "modules.community.another",
    )
    monkeypatch.setattr(runtime, "COMMUNITY_EXTENSIONS", configured)
    loaded = []

    async def load_extension(path):
        loaded.append(path)

    skipped = []
    asyncio.run(
        runtime._load_community_extensions(
            SimpleNamespace(load_extension=load_extension),
            failure_reason=None,
            skipped=skipped,
        )
    )

    assert loaded == list(configured)
    assert skipped == []
