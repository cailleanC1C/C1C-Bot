import asyncio
import logging
from types import SimpleNamespace

from modules.common import runtime as runtime_module


class _Bot:
    def is_closed(self):
        return False

    def is_ready(self):
        return True

    def get_cog(self, _name):
        return None


class _QuotaError(Exception):
    pass


def test_clan_ads_config_quota_skips_registration_without_startup_failure(
    monkeypatch, caplog
):
    from shared.sheets import cache_scheduler
    from shared.sheets import recruitment as recruitment_sheets
    from modules.common import feature_flags
    from modules.recruitment import clan_ads
    from modules.ops import server_map
    from modules.community.leagues import scheduler as leagues_scheduler
    from modules.community.fusion import scheduler as fusion_scheduler
    from modules.community.shard_tracker import scheduler as shard_scheduler
    from modules.community.reset_reminders import scheduler as reset_scheduler

    runtime = runtime_module.Runtime(_Bot())
    caplog.set_level(logging.WARNING)

    monkeypatch.setattr(
        runtime_module.shared_config,
        "features",
        SimpleNamespace(housekeeping_enabled=False, mirralith_overview_enabled=False),
    )
    monkeypatch.setattr(cache_scheduler, "ensure_cache_registration", lambda: None)

    def fake_register_refresh_job(owner, *, bucket, interval, cadence_label):
        job = owner.scheduler.every(hours=1, tag=bucket, name=f"{bucket}_refresh")
        return SimpleNamespace(bucket=bucket, cadence_label=cadence_label), job

    monkeypatch.setattr(
        cache_scheduler, "register_refresh_job", fake_register_refresh_job
    )
    monkeypatch.setenv("MIRRALITH_POST_CRON", "")

    async def fake_config_value(key, default=None):
        assert key == "C1C_AD_REFRESH_DAYS"
        return default

    monkeypatch.setattr(recruitment_sheets, "get_config_value_async", fake_config_value)
    monkeypatch.setattr(feature_flags, "is_enabled", lambda key: key == "clan_ads")

    async def fail_load_config(*, force=False):
        raise _QuotaError("Google Sheets 429 RESOURCE_EXHAUSTED")

    monkeypatch.setattr(clan_ads, "load_config", fail_load_config)
    monkeypatch.setattr(server_map, "schedule_server_map_job", lambda _runtime: None)
    monkeypatch.setattr(
        leagues_scheduler, "schedule_leagues_jobs", lambda _runtime: None
    )
    monkeypatch.setattr(fusion_scheduler, "schedule_fusion_jobs", lambda _runtime: None)
    monkeypatch.setattr(shard_scheduler, "schedule_shard_jobs", lambda _runtime: None)
    monkeypatch.setattr(
        reset_scheduler, "schedule_reset_reminder_jobs", lambda _runtime: None
    )

    asyncio.run(runtime._register_ready_schedulers_inner())

    assert all(
        getattr(job, "name", None) != "clan_ads" for job in runtime.scheduler.jobs
    )
    assert (
        "clan_ads scheduler config resolve hit Google Sheets quota/backoff"
        in caplog.text
    )
    assert "exception_type=_QuotaError" in caplog.text
    assert "without failing startup" in caplog.text


def test_optional_scheduler_quota_skip_returns_false_and_logs(caplog):
    runtime = runtime_module.Runtime(_Bot())
    caplog.set_level(logging.WARNING)

    registered = runtime._register_optional_scheduler(
        "reset_reminders",
        "Feature Toggle:reset_reminders",
        lambda: (_ for _ in ()).throw(_QuotaError("429 RESOURCE_EXHAUSTED")),
    )

    assert registered is False
    assert (
        "reset_reminders scheduler config resolve hit Google Sheets quota/backoff"
        in caplog.text
    )
    assert "config_source=Feature Toggle:reset_reminders" in caplog.text
    assert "exception_type=_QuotaError" in caplog.text
